# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import json
import os
import uuid
from threading import Thread
from typing import Sequence

import pytest
import pytest_asyncio
from google.cloud.alloydb.connector import AsyncConnector, IPTypes
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding
from PIL import Image
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from langchain_google_alloydb_pg import AlloyDBEngine, AlloyDBVectorStore, Column

DEFAULT_TABLE = "test_table" + str(uuid.uuid4())
DEFAULT_TABLE_SYNC = "test_table_sync" + str(uuid.uuid4())
CUSTOM_TABLE = "test-table-custom" + str(uuid.uuid4())
IMAGE_TABLE = "test_image_table" + str(uuid.uuid4())
IMAGE_TABLE_SYNC = "test_image_table_sync" + str(uuid.uuid4())
VECTOR_SIZE = 768

embeddings_service = DeterministicFakeEmbedding(size=VECTOR_SIZE)
host = os.environ["IP_ADDRESS"]

texts = ["foo", "bar", "baz"]
metadatas = [{"page": str(i), "source": "google.com"} for i in range(len(texts))]
docs = [
    Document(page_content=texts[i], metadata=metadatas[i]) for i in range(len(texts))
]

embeddings = [embeddings_service.embed_query(texts[i]) for i in range(len(texts))]


class FakeImageEmbedding(DeterministicFakeEmbedding):

    def embed_image(self, image_paths: list[str]) -> list[list[float]]:
        return [self.embed_query(f"Image Path: {path}") for path in image_paths]


image_embedding_service = FakeImageEmbedding(size=VECTOR_SIZE)


def get_env_var(key: str, desc: str) -> str:
    v = os.environ.get(key)
    if v is None:
        raise ValueError(f"Must set env var {key} to: {desc}")
    return v


async def aexecute(
    engine: AlloyDBEngine,
    query: str,
) -> None:
    async def run(engine, query):
        async with engine._pool.connect() as conn:
            await conn.execute(text(query))
            await conn.commit()

    await engine._run_as_async(run(engine, query))


async def afetch(engine: AlloyDBEngine, query: str) -> Sequence[RowMapping]:
    async def run(engine, query):
        async with engine._pool.connect() as conn:
            result = await conn.execute(text(query))
            result_map = result.mappings()
            result_fetch = result_map.fetchall()
        return result_fetch

    return await engine._run_as_async(run(engine, query))


@pytest.mark.asyncio(loop_scope="class")
class TestVectorStore:
    @pytest.fixture(scope="module")
    def db_project(self) -> str:
        return get_env_var("PROJECT_ID", "project id for google cloud")

    @pytest.fixture(scope="module")
    def db_region(self) -> str:
        return get_env_var("REGION", "region for AlloyDB instance")

    @pytest.fixture(scope="module")
    def db_cluster(self) -> str:
        return get_env_var("CLUSTER_ID", "cluster for AlloyDB")

    @pytest.fixture(scope="module")
    def db_instance(self) -> str:
        return get_env_var("INSTANCE_ID", "instance for AlloyDB")

    @pytest.fixture(scope="module")
    def db_name(self) -> str:
        return get_env_var("DATABASE_ID", "database name on AlloyDB instance")

    @pytest.fixture(scope="module")
    def user(self) -> str:
        return get_env_var("DB_USER", "database user for AlloyDB")

    @pytest.fixture(scope="module")
    def password(self) -> str:
        return get_env_var("DB_PASSWORD", "database password for AlloyDB")

    @pytest_asyncio.fixture(scope="class")
    async def engine(self, db_project, db_region, db_cluster, db_instance, db_name):
        engine = await AlloyDBEngine.afrom_instance(
            project_id=db_project,
            cluster=db_cluster,
            instance=db_instance,
            region=db_region,
            database=db_name,
        )

        yield engine
        await aexecute(engine, f'DROP TABLE IF EXISTS "{DEFAULT_TABLE}"')
        await engine.close()

    @pytest_asyncio.fixture(scope="class")
    async def vs(self, engine):
        await engine.ainit_vectorstore_table(DEFAULT_TABLE, VECTOR_SIZE)
        vs = await AlloyDBVectorStore.create(
            engine,
            embedding_service=embeddings_service,
            table_name=DEFAULT_TABLE,
        )
        yield vs

    @pytest_asyncio.fixture(scope="class")
    async def engine_sync(
        self, db_project, db_region, db_cluster, db_instance, db_name
    ):
        engine_sync = AlloyDBEngine.from_instance(
            project_id=db_project,
            cluster=db_cluster,
            instance=db_instance,
            region=db_region,
            database=db_name,
        )
        yield engine_sync

        await aexecute(engine_sync, f'DROP TABLE IF EXISTS "{DEFAULT_TABLE_SYNC}"')
        await engine_sync.close()

    @pytest_asyncio.fixture(scope="class")
    def vs_sync(self, engine_sync):
        engine_sync.init_vectorstore_table(DEFAULT_TABLE_SYNC, VECTOR_SIZE)

        vs = AlloyDBVectorStore.create_sync(
            engine_sync,
            embedding_service=embeddings_service,
            table_name=DEFAULT_TABLE_SYNC,
        )
        yield vs

    @pytest_asyncio.fixture(scope="class")
    async def vs_custom(self, engine):
        await engine.ainit_vectorstore_table(
            CUSTOM_TABLE,
            VECTOR_SIZE,
            id_column="myid",
            content_column="mycontent",
            embedding_column="myembedding",
            metadata_columns=[Column("page", "TEXT"), Column("source", "TEXT")],
            metadata_json_column="mymeta",
        )
        vs = await AlloyDBVectorStore.create(
            engine,
            embedding_service=embeddings_service,
            table_name=CUSTOM_TABLE,
            id_column="myid",
            content_column="mycontent",
            embedding_column="myembedding",
            metadata_columns=["page", "source"],
            metadata_json_column="mymeta",
        )
        yield vs
        await aexecute(engine, f'DROP TABLE IF EXISTS "{CUSTOM_TABLE}"')

    @pytest_asyncio.fixture(scope="class")
    def image_uris(self):
        red_uri = str(uuid.uuid4()).replace("-", "_") + "test_image_red.jpg"
        green_uri = str(uuid.uuid4()).replace("-", "_") + "test_image_green.jpg"
        blue_uri = str(uuid.uuid4()).replace("-", "_") + "test_image_blue.jpg"
        gcs_uri = "gs://github-repo/img/vision/google-cloud-next.jpeg"
        image = Image.new("RGB", (100, 100), color="red")
        image.save(red_uri)
        image = Image.new("RGB", (100, 100), color="green")
        image.save(green_uri)
        image = Image.new("RGB", (100, 100), color="blue")
        image.save(blue_uri)
        image_uris = [red_uri, green_uri, blue_uri, gcs_uri]
        yield image_uris
        for uri in image_uris:
            try:
                os.remove(uri)
            except FileNotFoundError:
                pass

    async def test_init_with_constructor(self, engine):
        with pytest.raises(Exception):
            AlloyDBVectorStore(
                engine,
                embedding_service=embeddings_service,
                table_name=CUSTOM_TABLE,
                id_column="myid",
                content_column="noname",
                embedding_column="myembedding",
                metadata_columns=["page", "source"],
                metadata_json_column="mymeta",
            )

    async def test_post_init(self, engine):
        with pytest.raises(ValueError):
            await AlloyDBVectorStore.create(
                engine,
                embedding_service=embeddings_service,
                table_name=CUSTOM_TABLE,
                id_column="myid",
                content_column="noname",
                embedding_column="myembedding",
                metadata_columns=["page", "source"],
                metadata_json_column="mymeta",
            )

    async def test_aadd_texts(self, engine, vs):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        await vs.aadd_texts(texts, ids=ids)
        results = await afetch(engine, f'SELECT * FROM "{DEFAULT_TABLE}"')
        assert len(results) == 3

        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        await vs.aadd_texts(texts, metadatas, ids)
        results = await afetch(engine, f'SELECT * FROM "{DEFAULT_TABLE}"')
        assert len(results) == 6
        await aexecute(engine, f'TRUNCATE TABLE "{DEFAULT_TABLE}"')

    async def test_cross_env_add_texts(self, engine, vs):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        vs.add_texts(texts, ids=ids)
        results = await afetch(engine, f'SELECT * FROM "{DEFAULT_TABLE}"')
        assert len(results) == 3
        vs.delete(ids)
        await aexecute(engine, f'TRUNCATE TABLE "{DEFAULT_TABLE}"')

    async def test_aadd_texts_edge_cases(self, engine, vs):
        texts = ["Taylor's", '"Swift"', "best-friend"]
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        await vs.aadd_texts(texts, ids=ids)
        results = await afetch(engine, f'SELECT * FROM "{DEFAULT_TABLE}"')
        assert len(results) == 3
        await aexecute(engine, f'TRUNCATE TABLE "{DEFAULT_TABLE}"')

    async def test_aadd_docs(self, engine, vs):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        await vs.aadd_documents(docs, ids=ids)
        results = await afetch(engine, f'SELECT * FROM "{DEFAULT_TABLE}"')
        assert len(results) == 3
        await aexecute(engine, f'TRUNCATE TABLE "{DEFAULT_TABLE}"')

    async def test_aadd_embeddings(self, engine, vs_custom):
        await vs_custom.aadd_embeddings(
            texts=texts, embeddings=embeddings, metadatas=metadatas
        )
        results = await afetch(engine, f'SELECT * FROM "{CUSTOM_TABLE}"')
        assert len(results) == 3
        assert results[0]["mycontent"] == "foo"
        assert results[0]["myembedding"]
        assert results[0]["page"] == "0"
        assert results[0]["source"] == "google.com"
        await aexecute(engine, f'TRUNCATE TABLE "{CUSTOM_TABLE}"')

    async def test_adelete(self, engine, vs):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        await vs.aadd_texts(texts, ids=ids)
        results = await afetch(engine, f'SELECT * FROM "{DEFAULT_TABLE}"')
        assert len(results) == 3
        # delete an ID
        await vs.adelete([ids[0]])
        results = await afetch(engine, f'SELECT * FROM "{DEFAULT_TABLE}"')
        assert len(results) == 2
        await aexecute(engine, f'TRUNCATE TABLE "{DEFAULT_TABLE}"')

    async def test_aadd_texts_custom(self, engine, vs_custom):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        await vs_custom.aadd_texts(texts, ids=ids)
        results = await afetch(engine, f'SELECT * FROM "{CUSTOM_TABLE}"')
        assert len(results) == 3
        assert results[0]["mycontent"] == "foo"
        assert results[0]["myembedding"]
        assert results[0]["page"] is None
        assert results[0]["source"] is None

        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        await vs_custom.aadd_texts(texts, metadatas, ids)
        results = await afetch(engine, f'SELECT * FROM "{CUSTOM_TABLE}"')
        assert len(results) == 6
        await aexecute(engine, f'TRUNCATE TABLE "{CUSTOM_TABLE}"')

    async def test_aadd_docs_custom(self, engine, vs_custom):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        docs = [
            Document(
                page_content=texts[i],
                metadata={"page": str(i), "source": "google.com"},
            )
            for i in range(len(texts))
        ]
        await vs_custom.aadd_documents(docs, ids=ids)

        results = await afetch(engine, f'SELECT * FROM "{CUSTOM_TABLE}"')
        assert len(results) == 3
        assert results[0]["mycontent"] == "foo"
        assert results[0]["myembedding"]
        assert results[0]["page"] == "0"
        assert results[0]["source"] == "google.com"
        await aexecute(engine, f'TRUNCATE TABLE "{CUSTOM_TABLE}"')

    async def test_aadd_images(self, engine_sync, image_uris):
        engine_sync.init_vectorstore_table(
            IMAGE_TABLE,
            VECTOR_SIZE,
            metadata_columns=[
                Column("image_id", "TEXT"),
                Column("source", "TEXT"),
            ],
        )
        vs = AlloyDBVectorStore.create_sync(
            engine_sync,
            embedding_service=image_embedding_service,
            table_name=IMAGE_TABLE,
            metadata_columns=["image_id", "source"],
            metadata_json_column="mymeta",
        )
        ids = [str(uuid.uuid4()) for i in range(len(image_uris))]
        metadatas = [
            {"image_id": str(i), "source": "google.com"} for i in range(len(image_uris))
        ]
        await vs.aadd_images(image_uris, metadatas, ids)
        results = await afetch(engine_sync, f'SELECT * FROM "{IMAGE_TABLE}"')
        assert len(results) == len(image_uris)
        assert results[0]["image_id"] == "0"
        assert results[0]["source"] == "google.com"
        await aexecute(engine_sync, f'DROP TABLE IF EXISTS "{IMAGE_TABLE}"')

    async def test_aadd_images_store_uri_only(self, engine_sync, image_uris):
        table_name = IMAGE_TABLE_SYNC + "_store_uri_only"
        engine_sync.init_vectorstore_table(
            table_name,
            VECTOR_SIZE,
            metadata_columns=[
                Column("image_id", "TEXT"),
                Column("source", "TEXT"),
            ],
            metadata_json_column="mymeta",
        )
        vs = AlloyDBVectorStore.create_sync(
            engine_sync,
            embedding_service=image_embedding_service,
            table_name=table_name,
            metadata_columns=["image_id", "source"],
            metadata_json_column="mymeta",
        )
        ids = [str(uuid.uuid4()) for i in range(len(image_uris))]
        metadatas = [
            {"image_id": str(i), "source": "google.com"} for i in range(len(image_uris))
        ]
        # Test the async method on the sync class
        await vs.aadd_images(image_uris, metadatas, ids, store_uri_only=True)
        results = await afetch(engine_sync, f'SELECT * FROM "{table_name}"')
        assert len(results) == len(image_uris)
        for i, result_row in enumerate(results):
            assert (
                result_row[vs._AlloyDBVectorStore__vs.content_column] == image_uris[i]
            )
            uri_embedding = embeddings_service.embed_query(image_uris[i])
            image_embedding = image_embedding_service.embed_image([image_uris[i]])[0]
            actual_embedding = json.loads(
                result_row[vs._AlloyDBVectorStore__vs.embedding_column]
            )
            assert actual_embedding != pytest.approx(uri_embedding)
            assert actual_embedding == pytest.approx(image_embedding)
            assert result_row["image_id"] == str(i)
            assert result_row["source"] == "google.com"
            assert (
                result_row[vs._AlloyDBVectorStore__vs.metadata_json_column]["image_uri"]
                == image_uris[i]
            )
        await aexecute(engine_sync, f'DROP TABLE IF EXISTS "{table_name}"')

    async def test_adelete_custom(self, engine, vs_custom):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        await vs_custom.aadd_texts(texts, ids=ids)
        results = await afetch(engine, f'SELECT * FROM "{CUSTOM_TABLE}"')
        content = [result["mycontent"] for result in results]
        assert len(results) == 3
        assert "foo" in content
        # delete an ID
        await vs_custom.adelete([ids[0]])
        results = await afetch(engine, f'SELECT * FROM "{CUSTOM_TABLE}"')
        content = [result["mycontent"] for result in results]
        assert len(results) == 2
        assert "foo" not in content
        await aexecute(engine, f'TRUNCATE TABLE "{CUSTOM_TABLE}"')

    async def test_add_docs(self, engine_sync, vs_sync):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        vs_sync.add_documents(docs, ids=ids)
        results = await afetch(engine_sync, f'SELECT * FROM "{DEFAULT_TABLE_SYNC}"')
        assert len(results) == 3
        vs_sync.delete(ids)
        await aexecute(engine_sync, f'TRUNCATE TABLE "{DEFAULT_TABLE_SYNC}"')

    async def test_add_texts(self, engine_sync, vs_sync):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        vs_sync.add_texts(texts, ids=ids)
        results = await afetch(engine_sync, f'SELECT * FROM "{DEFAULT_TABLE_SYNC}"')
        assert len(results) == 3
        await vs_sync.adelete(ids)
        await aexecute(engine_sync, f'TRUNCATE TABLE "{DEFAULT_TABLE_SYNC}"')

    async def test_add_images(self, engine_sync, image_uris):
        engine_sync.init_vectorstore_table(IMAGE_TABLE_SYNC, VECTOR_SIZE)
        vs = AlloyDBVectorStore.create_sync(
            engine_sync,
            embedding_service=image_embedding_service,
            table_name=IMAGE_TABLE_SYNC,
        )

        ids = [str(uuid.uuid4()) for i in range(len(image_uris))]
        vs.add_images(image_uris, ids=ids)
        results = await afetch(engine_sync, (f'SELECT * FROM "{IMAGE_TABLE_SYNC}"'))
        assert len(results) == len(image_uris)
        await vs.adelete(ids)
        await aexecute(engine_sync, f'DROP TABLE IF EXISTS "{IMAGE_TABLE_SYNC}"')

    async def test_add_images_store_uri_only(self, engine_sync, image_uris):
        table_name = IMAGE_TABLE_SYNC + "_store_uri_only"
        engine_sync.init_vectorstore_table(
            table_name,
            VECTOR_SIZE,
            metadata_columns=[Column("image_id", "TEXT"), Column("source", "TEXT")],
            metadata_json_column="mymeta",
        )
        vs = AlloyDBVectorStore.create_sync(
            engine_sync,
            embedding_service=image_embedding_service,
            table_name=table_name,
            metadata_columns=["image_id", "source"],
            metadata_json_column="mymeta",
        )

        ids = [str(uuid.uuid4()) for i in range(len(image_uris))]
        metadatas = [
            {"image_id": str(i), "source": "google.com"} for i in range(len(image_uris))
        ]
        vs.add_images(image_uris, metadatas, ids, store_uri_only=True)
        results = await afetch(engine_sync, (f'SELECT * FROM "{table_name}"'))
        assert len(results) == len(image_uris)
        for i, result_row in enumerate(results):
            assert (
                result_row[vs._AlloyDBVectorStore__vs.content_column] == image_uris[i]
            )
            uri_embedding = embeddings_service.embed_query(image_uris[i])
            image_embedding = image_embedding_service.embed_image([image_uris[i]])[0]
            actual_embedding = json.loads(
                result_row[vs._AlloyDBVectorStore__vs.embedding_column]
            )
            assert actual_embedding != pytest.approx(uri_embedding)
            assert actual_embedding == pytest.approx(image_embedding)
            assert result_row["image_id"] == str(i)
            assert result_row["source"] == "google.com"
            assert (
                result_row[vs._AlloyDBVectorStore__vs.metadata_json_column]["image_uri"]
                == image_uris[i]
            )
        await vs.adelete(ids)
        await aexecute(engine_sync, f'DROP TABLE IF EXISTS "{table_name}"')

    async def test_cross_env(self, engine_sync, vs_sync):
        ids = [str(uuid.uuid4()) for i in range(len(texts))]
        await vs_sync.aadd_texts(texts, ids=ids)
        results = await afetch(engine_sync, f'SELECT * FROM "{DEFAULT_TABLE_SYNC}"')
        assert len(results) == 3
        await vs_sync.adelete(ids)
        await aexecute(engine_sync, f'TRUNCATE TABLE "{DEFAULT_TABLE_SYNC}"')

    async def test_add_embeddings(self, engine_sync, vs_custom):
        vs_custom.add_embeddings(
            texts=texts,
            embeddings=embeddings,
            metadatas=[
                {"page": str(i), "source": "google.com"} for i in range(len(texts))
            ],
        )
        results = await afetch(engine_sync, f'SELECT * FROM "{CUSTOM_TABLE}"')
        assert len(results) == 3
        assert results[0]["mycontent"] == "foo"
        assert results[0]["myembedding"]
        assert results[0]["page"] == "0"
        assert results[0]["source"] == "google.com"
        await aexecute(engine_sync, f'TRUNCATE TABLE "{CUSTOM_TABLE}"')

    async def test_create_vectorstore_with_invalid_parameters(self, engine):
        with pytest.raises(ValueError):
            await AlloyDBVectorStore.create(
                engine,
                embedding_service=embeddings_service,
                table_name=CUSTOM_TABLE,
                id_column="myid",
                content_column="mycontent",
                embedding_column="myembedding",
                metadata_columns=["random_column"],  # invalid metadata column
            )
        with pytest.raises(ValueError):
            await AlloyDBVectorStore.create(
                engine,
                embedding_service=embeddings_service,
                table_name=CUSTOM_TABLE,
                id_column="myid",
                content_column="langchain_id",  # invalid content column type
                embedding_column="myembedding",
                metadata_columns=["random_column"],
            )
        with pytest.raises(ValueError):
            await AlloyDBVectorStore.create(
                engine,
                embedding_service=embeddings_service,
                table_name=CUSTOM_TABLE,
                id_column="myid",
                content_column="mycontent",
                embedding_column="random_column",  # invalid embedding column
                metadata_columns=["random_column"],
            )
        with pytest.raises(ValueError):
            await AlloyDBVectorStore.create(
                engine,
                embedding_service=embeddings_service,
                table_name=CUSTOM_TABLE,
                id_column="myid",
                content_column="mycontent",
                embedding_column="langchain_id",  # invalid embedding column data type
                metadata_columns=["random_column"],
            )

    async def test_from_engine(
        self,
        db_project,
        db_region,
        db_cluster,
        db_instance,
        db_name,
        user,
        password,
    ):
        async with AsyncConnector() as connector:

            async def getconn():
                conn = await connector.connect(  # type: ignore
                    f"projects/{db_project}/locations/{db_region}/clusters/{db_cluster}/instances/{db_instance}",
                    "asyncpg",
                    user=user,
                    password=password,
                    db=db_name,
                    enable_iam_auth=False,
                    ip_type=IPTypes.PUBLIC,
                )
                return conn

            engine = create_async_engine(
                "postgresql+asyncpg://",
                async_creator=getconn,
            )

            engine = AlloyDBEngine.from_engine(engine)
            table_name = "test_table" + str(uuid.uuid4()).replace("-", "_")
            await engine.ainit_vectorstore_table(table_name, VECTOR_SIZE)
            vs = await AlloyDBVectorStore.create(
                engine,
                embedding_service=embeddings_service,
                table_name=table_name,
            )
            await vs.aadd_texts(["foo"])
            results = await afetch(engine, f"SELECT * FROM {table_name}")
            assert len(results) == 1

            await aexecute(engine, f"DROP TABLE {table_name}")
            await engine.close()

    async def test_from_engine_loop_connector(
        self,
        db_project,
        db_region,
        db_cluster,
        db_instance,
        db_name,
        user,
        password,
    ):
        async def init_connection_pool(
            # connector: AsyncConnector,
        ) -> AsyncEngine:
            connector = AsyncConnector()

            async def getconn():
                conn = await connector.connect(
                    f"projects/{db_project}/locations/{db_region}/clusters/{db_cluster}/instances/{db_instance}",
                    "asyncpg",
                    user=user,
                    password=password,
                    db=db_name,
                    enable_iam_auth=False,
                    ip_type="PUBLIC",
                )
                return conn

            pool = create_async_engine(
                "postgresql+asyncpg://",
                async_creator=getconn,
            )
            return pool

        loop = asyncio.new_event_loop()
        thread = Thread(target=loop.run_forever, daemon=True)
        thread.start()

        # connector = AsyncConnector()
        # coro = init_connection_pool(connector)
        coro = init_connection_pool()
        pool = asyncio.run_coroutine_threadsafe(coro, loop).result()
        engine = AlloyDBEngine.from_engine(pool, loop)
        table_name = "test_table" + str(uuid.uuid4()).replace("-", "_")
        await engine.ainit_vectorstore_table(table_name, VECTOR_SIZE)
        vs = await AlloyDBVectorStore.create(
            engine,
            embedding_service=embeddings_service,
            table_name=table_name,
        )
        await vs.aadd_texts(["foo"])
        vs.add_texts(["foo"])
        results = await afetch(engine, f"SELECT * FROM {table_name}")
        assert len(results) == 2

        await aexecute(engine, f"TRUNCATE TABLE {table_name}")
        await engine.close()

        vs = AlloyDBVectorStore.create_sync(
            engine,
            embedding_service=embeddings_service,
            table_name=table_name,
        )
        await vs.aadd_texts(["foo"])
        vs.add_texts(["foo"])
        results = await afetch(engine, f"SELECT * FROM {table_name}")
        assert len(results) == 2

        await aexecute(engine, f"DROP TABLE {table_name}")

    async def test_from_engine_args_url(
        self,
        db_name,
        user,
        password,
    ):
        port = "5432"
        url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"
        engine = AlloyDBEngine.from_engine_args(url)
        table_name = "test_table" + str(uuid.uuid4()).replace("-", "_")
        await engine.ainit_vectorstore_table(table_name, VECTOR_SIZE)
        vs = await AlloyDBVectorStore.create(
            engine,
            embedding_service=embeddings_service,
            table_name=table_name,
        )
        await vs.aadd_texts(["foo"])
        vs.add_texts(["foo"])
        results = await afetch(engine, f"SELECT * FROM {table_name}")
        assert len(results) == 2

        await aexecute(engine, f"TRUNCATE TABLE {table_name}")
        vs = AlloyDBVectorStore.create_sync(
            engine,
            embedding_service=embeddings_service,
            table_name=table_name,
        )
        await vs.aadd_texts(["foo"])
        vs.add_texts(["bar"])
        results = await afetch(engine, f"SELECT * FROM {table_name}")
        assert len(results) == 2
        await aexecute(engine, f"DROP TABLE {table_name}")
        await engine.close()

    async def test_from_engine_loop(
        self,
        db_name,
        user,
        password,
    ):
        port = "5432"
        url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"

        loop = asyncio.new_event_loop()
        thread = Thread(target=loop.run_forever, daemon=True)
        thread.start()
        pool = create_async_engine(url)
        engine = AlloyDBEngine.from_engine(pool, loop)

        table_name = "test_table" + str(uuid.uuid4()).replace("-", "_")
        await engine.ainit_vectorstore_table(table_name, VECTOR_SIZE)
        vs = await AlloyDBVectorStore.create(
            engine,
            embedding_service=embeddings_service,
            table_name=table_name,
        )
        await vs.aadd_texts(["foo"])
        vs.add_texts(["foo"])
        results = await afetch(engine, f"SELECT * FROM {table_name}")
        assert len(results) == 2

        await aexecute(engine, f"TRUNCATE TABLE {table_name}")
        vs = AlloyDBVectorStore.create_sync(
            engine,
            embedding_service=embeddings_service,
            table_name=table_name,
        )
        await vs.aadd_texts(["foo"])
        vs.add_texts(["bar"])
        results = await afetch(engine, f"SELECT * FROM {table_name}")
        assert len(results) == 2
        await aexecute(engine, f"DROP TABLE {table_name}")
        await engine.close()

    def test_get_table_name(self, vs):
        assert vs.get_table_name() == DEFAULT_TABLE
