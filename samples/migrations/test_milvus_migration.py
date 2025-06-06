# Copyright 2025 Google LLC
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

import os
import uuid
from typing import Sequence

import pytest
import pytest_asyncio
from google.cloud.alloydb.connector import IPTypes
from langchain_core.documents import Document
from langchain_core.embeddings import FakeEmbeddings
from langchain_milvus import Milvus  # type: ignore
from migrate_milvus_vectorstore_to_alloydb import main
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping

from langchain_google_alloydb_pg import AlloyDBEngine

DEFAULT_TABLE = "test_milvus_migration" + str(uuid.uuid4())
PERSISTENT_DB_PATH = "./milvus_data.db"
EMBEDDING_SERVICE = FakeEmbeddings(size=768)


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


def create_milvus_collection(collection):
    vector_store = Milvus(
        embedding_function=EMBEDDING_SERVICE,
        connection_args={"uri": PERSISTENT_DB_PATH},
        collection_name=collection,
        index_params={
            "index_type": "IVF_FLAT",  # defaults to HNSW but local mode supports IVF_FLAT
        },
    )

    uuids = [str(uuid.uuid4()) for i in range(1000)]
    documents = [
        Document(page_content=f"content#{i}", metadata={"idv": f"{i}"})
        for i in range(1000)
    ]

    vector_store.add_documents(documents=documents, ids=uuids)


@pytest.mark.asyncio(loop_scope="class")
class TestMigrations:
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
    def db_user(self) -> str:
        return get_env_var("DB_USER", "database user for AlloyDB")

    @pytest.fixture(scope="module")
    def db_password(self) -> str:
        return get_env_var("DB_PASSWORD", "database password for AlloyDB")

    @pytest.fixture(scope="module")
    def milvus_collection_name(self) -> str:
        return get_env_var(
            "MILVUS_COLLECTION_NAME", "collection name for milvus instance"
        )

    @pytest_asyncio.fixture(scope="class")
    async def engine(
        self,
        db_project,
        db_region,
        db_cluster,
        db_instance,
        db_name,
        db_user,
        db_password,
    ):
        engine = await AlloyDBEngine.afrom_instance(
            project_id=db_project,
            cluster=db_cluster,
            instance=db_instance,
            region=db_region,
            database=db_name,
            user=db_user,
            password=db_password,
            ip_type=IPTypes.PUBLIC,
        )

        yield engine
        await aexecute(engine, f'DROP TABLE IF EXISTS "{DEFAULT_TABLE}"')
        await engine.close()

    async def test_milvus(
        self,
        engine,
        capsys,
        milvus_collection_name,
        db_project,
        db_region,
        db_cluster,
        db_instance,
        db_name,
        db_user,
        db_password,
    ):
        create_milvus_collection(milvus_collection_name)

        await main(
            milvus_collection_name=milvus_collection_name,
            milvus_uri=PERSISTENT_DB_PATH,
            vector_size=768,
            milvus_batch_size=50,
            project_id=db_project,
            region=db_region,
            cluster=db_cluster,
            instance=db_instance,
            alloydb_table=DEFAULT_TABLE,
            db_name=db_name,
            db_user=db_user,
            db_pwd=db_password,
        )

        out, err = capsys.readouterr()

        # Assert on the script's output
        assert "Error" not in err  # Check for errors
        assert "Milvus client initiated." in out
        assert "Langchain AlloyDB client initiated" in out
        assert "Langchain Fake Embeddings service initiated." in out
        assert "Langchain AlloyDB vectorstore table created" in out
        assert "Langchain AlloyDBVectorStore initialized" in out
        assert "Milvus client fetched all data from collection." in out
        assert "Migration completed, inserted all the batches of data to AlloyDB" in out
        results = await afetch(engine, f'SELECT * FROM "{DEFAULT_TABLE}"')
        assert len(results) == 1000
