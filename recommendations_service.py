import logging
import os

import pandas as pd
import requests
from fastapi import FastAPI
from contextlib import asynccontextmanager

logger = logging.getLogger("uvicorn.error")

# --- URL внешних сервисов (запускаются отдельно) ---
features_store_url = os.environ.get("FEATURES_STORE_URL", "http://127.0.0.1:8010")
events_store_url = os.environ.get("EVENTS_STORE_URL", "http://127.0.0.1:8020")

# --- Конфигурация S3 ---
os.environ["AWS_ENDPOINT_URL"] = "https://storage.yandexcloud.net"
storage_options = {"client_kwargs": {"endpoint_url": "https://storage.yandexcloud.net"}}
bucket_path = "s3://s3-student-mle-20260512-e5accb6525-freetrack/recsys/recommendations/"


class Recommendations:

    def __init__(self):
        self._recs = {"personal": None, "default": None}
        self._stats = {
            "request_personal_count": 0,
            "request_default_count": 0,
        }

    def load(self, type, path, **kwargs):
        """
        Загружает рекомендации из файла
        """
        logger.info(f"Loading recommendations, type: {type}")
        self._recs[type] = pd.read_parquet(path, **kwargs)
        if type == "personal":
            self._recs[type] = self._recs[type].set_index("user_id")
        logger.info(f"Loaded")

    def get(self, user_id: int, k: int = 100):
        """
        Возвращает список рекомендаций (track_id) для пользователя,
        отсортированный по rank. Если персональных рекомендаций нет —
        отдаёт рекомендации по умолчанию (топ популярных).
        """
        try:
            recs = self._recs["personal"].loc[user_id]
            recs = recs.sort_values("rank")
            recs = recs["track_id"].to_list()[:k]
            self._stats["request_personal_count"] += 1
        except KeyError:
            recs = self._recs["default"].sort_values("rank")
            recs = recs["track_id"].to_list()[:k]
            self._stats["request_default_count"] += 1
        except Exception:
            logger.error("No recommendations found")
            recs = []

        return recs

    def stats(self):
        logger.info("Stats for recommendations")
        for name, value in self._stats.items():
            logger.info(f"{name:<30} {value} ")


rec_store = Recommendations()


def dedup_ids(ids):
    """
    Дедуплицирует список идентификаторов, оставляя только первое вхождение
    """
    seen = set()
    ids = [id for id in ids if not (id in seen or seen.add(id))]
    return ids


@asynccontextmanager
async def lifespan(app: FastAPI):
    # код ниже (до yield) выполнится только один раз при запуске сервиса
    logger.info("Starting")

    rec_store.load(
        "personal",
        bucket_path + "recommendations.parquet",
        columns=["user_id", "track_id", "rank"],
        storage_options=storage_options,
    )
    rec_store.load(
        "default",
        bucket_path + "top_popular.parquet",
        columns=["track_id", "rank"],
        storage_options=storage_options,
    )

    logger.info("Ready!")
    yield
    # этот код выполнится только один раз при остановке сервиса
    logger.info("Stopping")


app = FastAPI(title="recommendations", lifespan=lifespan)


@app.post("/recommendations_offline")
async def recommendations_offline(user_id: int, k: int = 100):
    """
    Возвращает список офлайн-рекомендаций длиной k для пользователя user_id.
    Учитывает историю пользователя за счёт использования персонализированного
    набора кандидатов, отранжированного CatBoost-моделью (по итоговому rank).
    При отсутствии персональных рекомендаций — top популярных треков.
    """
    recs = rec_store.get(user_id, k)
    return {"recs": recs}


@app.post("/recommendations_online")
async def recommendations_online(user_id: int, k: int = 100):
    """
    Возвращает список онлайн-рекомендаций длиной k для пользователя user_id,
    построенных на основе последних событий пользователя (session history)
    и i2i-похожих треков, посчитанных через ALS.
    """
    headers = {"Content-type": "application/json", "Accept": "text/plain"}

    # получаем список последних событий пользователя, возьмём три последних
    params = {"user_id": user_id, "k": 3}
    resp = requests.post(events_store_url + "/get", headers=headers, params=params)
    events = resp.json()
    events = events["events"]

    # получаем список треков, похожих на последние, с которыми взаимодействовал пользователь
    tracks = []
    scores = []
    for track_id in events:
        params = {"item_id": track_id, "k": k}
        resp = requests.post(features_store_url + "/similar_items", headers=headers, params=params)
        item_similar_items = resp.json()
        tracks += item_similar_items["item_id_2"]
        scores += item_similar_items["score"]

    # сортируем похожие объекты по scores в убывающем порядке
    combined = list(zip(tracks, scores))
    combined = sorted(combined, key=lambda x: x[1], reverse=True)
    combined = [track for track, _ in combined]

    # удаляем дубликаты, чтобы не выдавать одинаковые рекомендации
    recs = dedup_ids(combined)

    return {"recs": recs}


@app.post("/recommendations")
async def recommendations(user_id: int, k: int = 100):
    """
    Возвращает список смешанных (blended) рекомендаций длиной k для
    пользователя user_id: online- и offline-рекомендации чередуются 1:1
    (начиная с online), после чего дубликаты удаляются, а список обрезается
    до k элементов. Подробности стратегии — в README.
    """
    recs_offline = await recommendations_offline(user_id, k)
    recs_online = await recommendations_online(user_id, k)
    recs_offline = recs_offline["recs"]
    recs_online = recs_online["recs"]

    recs_blended = []
    min_length = min(len(recs_offline), len(recs_online))

    for i in range(min_length):
        recs_blended.append(recs_online[i])
        recs_blended.append(recs_offline[i])

    recs_blended += recs_offline[min_length:]
    recs_blended += recs_online[min_length:]

    recs_blended = dedup_ids(recs_blended)
    recs_blended = recs_blended[:k]

    return {"recs": recs_blended}


@app.post("/stats")
async def stats():
    """
    Возвращает статистику запросов персональных/дефолтных рекомендаций
    """
    rec_store.stats()
    return rec_store._stats