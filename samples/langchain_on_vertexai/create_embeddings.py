# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import uuid

from config import (
    CHAT_TABLE_NAME,
    CLUSTER,
    DATABASE,
    INSTANCE,
    PASSWORD,
    PROJECT_ID,
    REGION,
    TABLE_NAME,
    USER,
)
from google.cloud import resourcemanager_v3  # type: ignore
from langchain_community.document_loaders.csv_loader import CSVLoader
from langchain_google_vertexai import VertexAIEmbeddings
from sqlalchemy import text

from langchain_google_alloydb_pg import AlloyDBEngine, AlloyDBVectorStore


async def create_databases():
    engine = await AlloyDBEngine.afrom_instance(
        PROJECT_ID,
        REGION,
        CLUSTER,
        INSTANCE,
        database="postgres",
        user=USER,
        password=PASSWORD,
    )

    async with engine._pool.connect() as conn:
        await conn.execute(text("COMMIT"))
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{DATABASE}"'))
        await conn.execute(text("COMMIT"))
        await conn.execute(text(f'CREATE DATABASE "{DATABASE}"'))
    await engine.close()


async def create_vectorstore():
    engine = await AlloyDBEngine.afrom_instance(
        PROJECT_ID,
        REGION,
        CLUSTER,
        INSTANCE,
        DATABASE,
        user=USER,
        password=PASSWORD,
    )

    await engine.ainit_vectorstore_table(
        table_name=TABLE_NAME, vector_size=768, overwrite_existing=True
    )

    await engine.ainit_chat_history_table(table_name=CHAT_TABLE_NAME)

    rm = resourcemanager_v3.ProjectsClient()
    res = rm.get_project(
        request=resourcemanager_v3.GetProjectRequest(name=f"projects/{PROJECT_ID}")
    )
    project_number = res.name.split("/")[1]
    IAM_USER = f"service-{project_number}@gcp-sa-aiplatform-re.iam"

    async def grant_select(engine):
        async with engine._pool.connect() as conn:
            await conn.execute(text(f'GRANT SELECT ON {TABLE_NAME} TO "{IAM_USER}";'))
            await conn.commit()

    await engine._run_as_async(grant_select(engine))

    metadata = [
        "show_id",
        "type",
        "country",
        "date_added",
        "release_year",
        "rating",
        "duration",
        "listed_in",
    ]
    loader = CSVLoader(file_path="./movies.csv", metadata_columns=metadata)
    docs = loader.load()

    vector_store = await AlloyDBVectorStore.create(
        engine,
        table_name=TABLE_NAME,
        embedding_service=VertexAIEmbeddings(
            model_name="textembedding-gecko@latest", project=PROJECT_ID
        ),
    )

    ids = [str(uuid.uuid4()) for i in range(len(docs))]
    await vector_store.aadd_documents(docs, ids=ids)


async def main():
    await create_databases()
    await create_vectorstore()


asyncio.run(main())
