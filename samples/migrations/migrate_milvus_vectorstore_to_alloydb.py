#!/usr/bin/env python

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

import asyncio
from typing import Any, Iterator

from google.cloud.alloydb.connector import IPTypes

"""Migrate Milvus to Langchain AlloyDBVectorStore.
Given a Milvus collection, the following code fetches the data from Milvus
in batches and uploads to an AlloyDBVectorStore.
"""

# TODO(dev): Replace the values below
MILVUS_URI = "./milvus_example.db"
MILVUS_COLLECTION_NAME = "langchain_example"
PROJECT_ID = "my-project-id"
REGION = "us-central1"
CLUSTER = "my-cluster"
INSTANCE = "my-instance"
DB_NAME = "my-db"
DB_USER = "postgres"
DB_PWD = "secret-password"

# TODO(developer): Optional, change the values below.
VECTOR_SIZE = 768
MILVUS_BATCH_SIZE = 10
ALLOYDB_TABLE_NAME = "alloydb_table"
MAX_CONCURRENCY = 100

from pymilvus import MilvusClient  # type: ignore


def get_data_batch(
    milvus_client: MilvusClient,
    milvus_batch_size: int = MILVUS_BATCH_SIZE,
    milvus_collection_name: str = MILVUS_COLLECTION_NAME,
) -> Iterator[tuple[list[str], list[Any], list[list[float]], list[Any]]]:
    # [START milvus_get_data_batch]
    # Iterate through the IDs and download their contents
    iterator = milvus_client.query_iterator(
        collection_name=milvus_collection_name,
        filter='pk >= "0"',
        output_fields=["pk", "text", "vector", "idv"],
        batch_size=milvus_batch_size,
    )

    while True:
        ids = []
        content = []
        embeddings = []
        metadatas = []
        page = iterator.next()
        if len(page) == 0:
            iterator.close()
            break
        for i in range(len(page)):
            # You might need to update this data translation logic according to one or more of your field names
            doc = page[i]
            # pk is the unqiue identifier for the content
            ids.append(doc["pk"])
            # text is the content which was encoded
            content.append(doc["text"])
            # vector is the vector embedding of the content
            embeddings.append(doc["vector"])
            del doc["pk"]
            del doc["text"]
            del doc["vector"]
            # doc is the additional context
            metadatas.append(doc)
        yield ids, content, embeddings, metadatas
    # [END milvus_get_data_batch]
    print("Milvus client fetched all data from collection.")


async def main(
    milvus_collection_name: str = MILVUS_COLLECTION_NAME,
    vector_size: int = VECTOR_SIZE,
    milvus_batch_size: int = MILVUS_BATCH_SIZE,
    milvus_uri: str = MILVUS_URI,
    project_id: str = PROJECT_ID,
    region: str = REGION,
    cluster: str = CLUSTER,
    instance: str = INSTANCE,
    alloydb_table: str = ALLOYDB_TABLE_NAME,
    db_name: str = DB_NAME,
    db_user: str = DB_USER,
    db_pwd: str = DB_PWD,
    max_concurrency: int = MAX_CONCURRENCY,
) -> None:
    # [START milvus_get_client]
    milvus_client = MilvusClient(uri=milvus_uri)
    # [END milvus_get_client]
    print("Milvus client initiated.")

    # [START milvus_vectorstore_alloydb_migration_get_client]
    from langchain_google_alloydb_pg import AlloyDBEngine

    alloydb_engine = await AlloyDBEngine.afrom_instance(
        project_id=project_id,
        region=region,
        cluster=cluster,
        instance=instance,
        database=db_name,
        user=db_user,
        password=db_pwd,
        ip_type=IPTypes.PUBLIC,
    )
    # [END milvus_vectorstore_alloydb_migration_get_client]
    print("Langchain AlloyDB client initiated.")

    # [START milvus_vectorstore_alloydb_migration_embedding_service]
    # The VectorStore interface requires an embedding service. This workflow does not
    # generate new embeddings, therefore FakeEmbeddings class is used to avoid any costs.
    from langchain_core.embeddings import FakeEmbeddings

    embeddings_service = FakeEmbeddings(size=vector_size)
    # [END milvus_vectorstore_alloydb_migration_embedding_service]
    print("Langchain Fake Embeddings service initiated.")

    # [START milvus_vectorstore_alloydb_migration_create_table]
    await alloydb_engine.ainit_vectorstore_table(
        table_name=alloydb_table,
        vector_size=vector_size,
        # Customize the ID column types with `id_column` if not using the UUID data type
    )
    # [END milvus_vectorstore_alloydb_migration_create_table]
    print("Langchain AlloyDB vectorstore table created.")

    # [START milvus_vectorstore_alloydb_migration_vector_store]
    from langchain_google_alloydb_pg import AlloyDBVectorStore

    vs = await AlloyDBVectorStore.create(
        engine=alloydb_engine,
        embedding_service=embeddings_service,
        table_name=alloydb_table,
    )
    # [END milvus_vectorstore_alloydb_migration_vector_store]
    print("Langchain AlloyDBVectorStore initialized.")

    data_iterator = get_data_batch(
        milvus_client=milvus_client,
        milvus_batch_size=milvus_batch_size,
        milvus_collection_name=milvus_collection_name,
    )

    # [START milvus_vectorstore_alloydb_migration_insert_data_batch]
    pending: set[Any] = set()
    for ids, contents, embeddings, metadatas in data_iterator:
        pending.add(
            asyncio.ensure_future(
                vs.aadd_embeddings(
                    texts=contents,
                    embeddings=embeddings,
                    metadatas=metadatas,
                    ids=ids,
                )
            )
        )
        if len(pending) >= max_concurrency:
            _, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
    if pending:
        await asyncio.wait(pending)
    # [END milvus_vectorstore_alloydb_migration_insert_data_batch]
    print("Migration completed, inserted all the batches of data to AlloyDB.")


if __name__ == "__main__":
    asyncio.run(main())
