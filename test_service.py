"""
Скрипт для тестирования микросервиса рекомендаций.
Проверяет три сценария:
  1. пользователь без персональных рекомендаций (только default + online),
  2. пользователь с персональными рекомендациями, но без онлайн-истории,
  3. пользователь с персональными рекомендациями и онлайн-историей.

Запуск (сервисы должны быть уже запущены):
    uvicorn features_service:app --port 8010 &
    uvicorn events_service:app --port 8020 &
    uvicorn recommendations_service:app --port 8000 &
    python test_service.py > test_service.log 2>&1
"""

import logging
import sys
import os
import pandas as pd
import requests

os.environ["AWS_ENDPOINT_URL"] = "https://storage.yandexcloud.net"
storage_options = {"client_kwargs": {"endpoint_url": "https://storage.yandexcloud.net"}}



logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

recommendations_url = "http://127.0.0.1:8000"
events_store_url = "http://127.0.0.1:8020"
headers = {"Content-type": "application/json", "Accept": "text/plain"}

catalog_names = pd.read_parquet("data/catalog_names.parquet")
items = pd.read_parquet(
    "s3://s3-student-mle-20260512-e5accb6525-freetrack/recsys/data/items.parquet",
    storage_options=storage_options
)

# --- функция для читаемого названия трека ---
def get_track_info(track_id, items, catalog_names):
    name_row = catalog_names[(catalog_names["id"] == track_id) & (catalog_names["type"] == "track")]
    track_name = name_row["name"].values[0] if len(name_row) > 0 else "Unknown"

    artist_ids_row = items.loc[items["track_id"] == track_id, "artists"]
    if len(artist_ids_row) > 0:
        artist_ids = artist_ids_row.values[0]
        artist_names = catalog_names.loc[
            (catalog_names["id"].isin(artist_ids)) & (catalog_names["type"] == "artist"), "name"
        ].tolist()
    else:
        artist_names = ["Unknown"]

    return f"{track_name} — {', '.join(artist_names)}"


def format_track_list(track_ids, items, catalog_names, limit=5):
    lines = []
    for tid in track_ids[:limit]:
        lines.append(f"    [{tid}] {get_track_info(tid, items, catalog_names)}")
    return "\n" + "\n".join(lines) if lines else "    (пусто)"

# --- пользователи для тестовых сценариев ---
similar = pd.read_parquet(
    "s3://s3-student-mle-20260512-e5accb6525-freetrack/recsys/recommendations/similar.parquet",
    storage_options=storage_options
)
recs = pd.read_parquet(
    "s3://s3-student-mle-20260512-e5accb6525-freetrack/recsys/recommendations/recommendations.parquet",
    storage_options=storage_options
)
# берём реальные track_id, для которых есть i2i-рекомендации
valid_history_tracks = similar["track_id_1"].drop_duplicates().sample(3, random_state=0).tolist()

# берём реального пользователя с персональными рекомендациями
valid_personal_user = recs["user_id"].sample(1, random_state=0).values[0]
valid_user = recs["user_id"].sample(1, random_state=9).values[0]

NO_PERSONAL_USER_ID = 999_999_999          # заведомо отсутствует в recommendations.parquet
PERSONAL_NO_HISTORY_USER_ID = valid_personal_user            
PERSONAL_WITH_HISTORY_USER_ID = valid_user       

# реальные track_id для имитации истории прослушиваний
HISTORY_TRACK_IDS = valid_history_tracks


def call_recommendations(user_id: int, k: int = 10):
    params = {"user_id": user_id, "k": k}
    resp = requests.post(recommendations_url + "/recommendations", headers=headers, params=params)
    return resp.status_code, resp.json()


def call_offline(user_id: int, k: int = 10):
    params = {"user_id": user_id, "k": k}
    resp = requests.post(recommendations_url + "/recommendations_offline", headers=headers, params=params)
    return resp.status_code, resp.json()


def call_online(user_id: int, k: int = 10):
    params = {"user_id": user_id, "k": k}
    resp = requests.post(recommendations_url + "/recommendations_online", headers=headers, params=params)
    return resp.status_code, resp.json()


def put_event(user_id: int, item_id: int):
    params = {"user_id": user_id, "item_id": item_id}
    resp = requests.post(events_store_url + "/put", headers=headers, params=params)
    return resp.status_code, resp.json()


def run_scenario_1():
    logger.info("=" * 70)
    logger.info("СЦЕНАРИЙ 1: пользователь без персональных рекомендаций")
    logger.info(f"user_id = {NO_PERSONAL_USER_ID}")

    status, offline = call_offline(NO_PERSONAL_USER_ID)
    logger.info(f"/recommendations_offline -> status={status}, всего {len(offline['recs'])}")
    logger.info("Треки:" + format_track_list(offline["recs"], items, catalog_names))

    status, online = call_online(NO_PERSONAL_USER_ID)
    logger.info(f"/recommendations_online -> status={status}, всего {len(online['recs'])}")

    status, blended = call_recommendations(NO_PERSONAL_USER_ID)
    logger.info(f"/recommendations (blended) -> status={status}, всего {len(blended['recs'])}")
    logger.info("Треки:" + format_track_list(blended["recs"], items, catalog_names))

    assert status == 200
    assert len(blended["recs"]) > 0
    logger.info("Сценарий 1: PASSED")


def run_scenario_2():
    logger.info("=" * 70)
    logger.info("СЦЕНАРИЙ 2: пользователь с персональными рекомендациями, но без онлайн-истории")
    logger.info(f"user_id = {PERSONAL_NO_HISTORY_USER_ID}")

    status, offline = call_offline(PERSONAL_NO_HISTORY_USER_ID)
    logger.info(f"/recommendations_offline -> status={status}, recs={offline['recs'][:5]}... (всего {len(offline['recs'])})")
    logger.info("Треки:" + format_track_list(offline["recs"], items, catalog_names))


    status, online = call_online(PERSONAL_NO_HISTORY_USER_ID)
    logger.info(f"/recommendations_online -> status={status}, recs={online['recs']} (ожидается пустой список)")

    status, blended = call_recommendations(PERSONAL_NO_HISTORY_USER_ID)
    logger.info(f"/recommendations (blended) -> status={status}, recs={blended['recs'][:10]}... (всего {len(blended['recs'])})")
    logger.info("Треки:" + format_track_list(offline["recs"], items, catalog_names))

    assert status == 200
    assert len(online["recs"]) == 0, "Ожидалась пустая онлайн-история для этого пользователя"
    assert blended["recs"] == offline["recs"][:len(blended["recs"])], \
        "При отсутствии онлайн-истории blended должен совпадать с offline"
    logger.info("Сценарий 2: PASSED — при пустой онлайн-истории blended = offline")


def run_scenario_3():
    logger.info("=" * 70)
    logger.info("СЦЕНАРИЙ 3: пользователь с персональными рекомендациями и онлайн-историей")
    logger.info(f"user_id = {PERSONAL_WITH_HISTORY_USER_ID}")

    logger.info(f"Отправляем историю прослушиваний: {HISTORY_TRACK_IDS}")
    for track_id in HISTORY_TRACK_IDS:
        status, result = put_event(PERSONAL_WITH_HISTORY_USER_ID, track_id)
        logger.info(f"/put -> status={status}, result={result}")

    status, offline = call_offline(PERSONAL_WITH_HISTORY_USER_ID)
    logger.info(f"/recommendations_offline -> status={status}, recs={offline['recs'][:5]}... (всего {len(offline['recs'])})")
    logger.info("Треки:" + format_track_list(offline["recs"], items, catalog_names))


    status, online = call_online(PERSONAL_WITH_HISTORY_USER_ID)
    logger.info(f"/recommendations_online -> status={status}, recs={online['recs'][:5]}... (всего {len(online['recs'])})")

    status, blended = call_recommendations(PERSONAL_WITH_HISTORY_USER_ID)
    logger.info(f"/recommendations (blended) -> status={status}, recs={blended['recs'][:10]}... (всего {len(blended['recs'])})")
    logger.info("Треки:" + format_track_list(offline["recs"], items, catalog_names))

    assert status == 200
    assert len(online["recs"]) > 0, "Ожидалась непустая онлайн-история после /put"
    assert len(blended["recs"]) > 0
    logger.info("Сценарий 3: PASSED — blended сочетает online и offline рекомендации")


def main():
    logger.info("Запуск тестирования микросервиса рекомендаций")
    try:
        run_scenario_1()
        run_scenario_2()
        run_scenario_3()
        logger.info("=" * 70)
        logger.info("ВСЕ СЦЕНАРИИ ПРОЙДЕНЫ УСПЕШНО")
    except AssertionError as e:
        logger.error(f"Тест не пройден: {e}")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Не удалось подключиться к сервису. Убедитесь, что все сервисы запущены: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
