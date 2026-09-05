import logging
import os
from contextlib import asynccontextmanager

import pandas as pd
from fastapi import FastAPI

logger = logging.getLogger("uvicorn.error")

os.environ["AWS_ENDPOINT_URL"] = "https://storage.yandexcloud.net"
storage_options = {"client_kwargs": {"endpoint_url": "https://storage.yandexcloud.net"}}
bucket_path = "s3://s3-student-mle-20260512-e5accb6525-freetrack/recsys/recommendations/"


class SimilarItems:

    def __init__(self):
        self._similar_items = None

    def load(self, path, **kwargs):
        """
        Загружаем данные из файла
        """
        logger.info(f"Loading data")
        self._similar_items = pd.read_parquet(path, **kwargs)
        self._similar_items = self._similar_items.set_index("track_id_1")
        logger.info(f"Loaded")

    def get(self, item_id: int, k: int = 10):
        """
        Возвращает список похожих объектов
        """
        try:
            i2i = self._similar_items.loc[item_id].head(k)
            i2i = i2i[["track_id_2", "score"]].rename(columns={"track_id_2": "item_id_2"})
            i2i = i2i.to_dict(orient="list")
        except KeyError:
            logger.error("No recommendations found")
            i2i = {"item_id_2": [], "score": []}

        return i2i


sim_items_store = SimilarItems()

@asynccontextmanager
async def lifespan(app: FastAPI):
    sim_items_store.load(
        bucket_path + "similar.parquet",
        columns=["track_id_1", "track_id_2", "score"],
        storage_options=storage_options,
    )
    logger.info("Ready!")
    yield

app = FastAPI(title="features", lifespan=lifespan)

@app.post("/similar_items")
async def recommendations(item_id: int, k: int = 10):
    """
    Возвращает список похожих объектов длиной k для item_id
    """
    i2i = sim_items_store.get(item_id, k)
    return i2i