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

# TODO: Remove below import when minimum supported Python version is 3.10
from __future__ import annotations

import base64
import copy
import json
import re
import uuid
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np
import requests
from google.cloud import storage  # type: ignore
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore, utils
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncEngine

from .engine import AlloyDBEngine
from .indexes import (
    DEFAULT_DISTANCE_STRATEGY,
    DEFAULT_INDEX_NAME_SUFFIX,
    BaseIndex,
    DistanceStrategy,
    ExactNearestNeighbor,
    QueryOptions,
    ScaNNIndex,
)

COMPARISONS_TO_NATIVE = {
    "$eq": "=",
    "$ne": "!=",
    "$lt": "<",
    "$lte": "<=",
    "$gt": ">",
    "$gte": ">=",
}

SPECIAL_CASED_OPERATORS = {
    "$in",
    "$nin",
    "$between",
    "$exists",
}

TEXT_OPERATORS = {
    "$like",
    "$ilike",
}

LOGICAL_OPERATORS = {"$and", "$or", "$not"}

SUPPORTED_OPERATORS = (
    set(COMPARISONS_TO_NATIVE)
    .union(TEXT_OPERATORS)
    .union(LOGICAL_OPERATORS)
    .union(SPECIAL_CASED_OPERATORS)
)


class AsyncAlloyDBVectorStore(VectorStore):
    """Google AlloyDB Vector Store class"""

    __create_key = object()

    def __init__(
        self,
        key: object,
        engine: AsyncEngine,
        embedding_service: Embeddings,
        table_name: str,
        schema_name: str = "public",
        content_column: str = "content",
        embedding_column: str = "embedding",
        metadata_columns: list[str] = [],
        id_column: str = "langchain_id",
        metadata_json_column: Optional[str] = "langchain_metadata",
        distance_strategy: DistanceStrategy = DEFAULT_DISTANCE_STRATEGY,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        index_query_options: Optional[QueryOptions] = None,
    ):
        """AsyncAlloyDBVectorStore constructor.
        Args:
            key (object): Prevent direct constructor usage.
            engine (AlloyDBEngine): Connection pool engine for managing connections to AlloyDB database.
            embedding_service (Embeddings): Text embedding model to use.
            table_name (str): Name of the existing table or the table to be created.
            schema_name (str, optional): Name of the database schema. Defaults to "public".
            content_column (str): Column that represent a Document’s page_content. Defaults to "content".
            embedding_column (str): Column for embedding vectors. The embedding is generated from the document value. Defaults to "embedding".
            metadata_columns (list[str]): Column(s) that represent a document's metadata.
            id_column (str): Column that represents the Document's id. Defaults to "langchain_id".
            metadata_json_column (str): Column to store metadata as JSON. Defaults to "langchain_metadata".
            distance_strategy (DistanceStrategy): Distance strategy to use for vector similarity search. Defaults to COSINE_DISTANCE.
            k (int): Number of Documents to return from search. Defaults to 4.
            fetch_k (int): Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult (float): Number between 0 and 1 that determines the degree of diversity among the results with 0 corresponding to maximum diversity and 1 to minimum diversity. Defaults to 0.5.
            index_query_options (QueryOptions): Index query option.


        Raises:
            Exception: If called directly by user.
        """
        if key != AsyncAlloyDBVectorStore.__create_key:
            raise Exception(
                "Only create class through 'create' or 'create_sync' methods!"
            )

        self.engine = engine
        self.embedding_service = embedding_service
        self.table_name = table_name
        self.schema_name = schema_name
        self.content_column = content_column
        self.embedding_column = embedding_column
        self.metadata_columns = metadata_columns
        self.id_column = id_column
        self.metadata_json_column = metadata_json_column
        self.distance_strategy = distance_strategy
        self.k = k
        self.fetch_k = fetch_k
        self.lambda_mult = lambda_mult
        self.index_query_options = index_query_options

    @classmethod
    async def create(
        cls: type[AsyncAlloyDBVectorStore],
        engine: AlloyDBEngine,
        embedding_service: Embeddings,
        table_name: str,
        schema_name: str = "public",
        content_column: str = "content",
        embedding_column: str = "embedding",
        metadata_columns: list[str] = [],
        ignore_metadata_columns: Optional[list[str]] = None,
        id_column: str = "langchain_id",
        metadata_json_column: Optional[str] = "langchain_metadata",
        distance_strategy: DistanceStrategy = DEFAULT_DISTANCE_STRATEGY,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        index_query_options: Optional[QueryOptions] = None,
    ) -> AsyncAlloyDBVectorStore:
        """Create an AsyncAlloyDBVectorStore instance.

        Args:
            engine (AlloyDBEngine): Connection pool engine for managing connections to AlloyDB database.
            embedding_service (Embeddings): Text embedding model to use.
            table_name (str): Name of an existing table.
            schema_name (str, optional): Name of the database schema. Defaults to "public".
            content_column (str): Column that represent a Document’s page_content. Defaults to "content".
            embedding_column (str): Column for embedding vectors. The embedding is generated from the document value. Defaults to "embedding".
            metadata_columns (list[str]): Column(s) that represent a document's metadata.
            ignore_metadata_columns (list[str]): Column(s) to ignore in pre-existing tables for a document's metadata. Can not be used with metadata_columns. Defaults to None.
            id_column (str): Column that represents the Document's id. Defaults to "langchain_id".
            metadata_json_column (str): Column to store metadata as JSON. Defaults to "langchain_metadata".
            distance_strategy (DistanceStrategy): Distance strategy to use for vector similarity search. Defaults to COSINE_DISTANCE.
            k (int): Number of Documents to return from search. Defaults to 4.
            fetch_k (int): Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult (float): Number between 0 and 1 that determines the degree of diversity among the results with 0 corresponding to maximum diversity and 1 to minimum diversity. Defaults to 0.5.
            index_query_options (QueryOptions): Index query option.

        Returns:
            AsyncAlloyDBVectorStore
        """
        if metadata_columns and ignore_metadata_columns:
            raise ValueError(
                "Can not use both metadata_columns and ignore_metadata_columns."
            )
        # Get field type information
        stmt = f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{table_name}' AND table_schema = '{schema_name}'"
        async with engine._pool.connect() as conn:
            result = await conn.execute(text(stmt))
            result_map = result.mappings()
            results = result_map.fetchall()
        columns = {}
        for field in results:
            columns[field["column_name"]] = field["data_type"]

        # Check columns
        if id_column not in columns:
            raise ValueError(f"Id column, {id_column}, does not exist.")
        if content_column not in columns:
            raise ValueError(f"Content column, {content_column}, does not exist.")
        content_type = columns[content_column]
        if content_type != "text" and "char" not in content_type:
            raise ValueError(
                f"Content column, {content_column}, is type, {content_type}. It must be a type of character string."
            )
        if embedding_column not in columns:
            raise ValueError(f"Embedding column, {embedding_column}, does not exist.")
        if columns[embedding_column] != "USER-DEFINED":
            raise ValueError(
                f"Embedding column, {embedding_column}, is not type Vector."
            )

        metadata_json_column = (
            None if metadata_json_column not in columns else metadata_json_column
        )

        # If using metadata_columns check to make sure column exists
        for column in metadata_columns:
            if column not in columns:
                raise ValueError(f"Metadata column, {column}, does not exist.")

        # If using ignore_metadata_columns, filter out known columns and set known metadata columns
        all_columns = columns
        if ignore_metadata_columns:
            for column in ignore_metadata_columns:
                del all_columns[column]

            del all_columns[id_column]
            del all_columns[content_column]
            del all_columns[embedding_column]
            metadata_columns = [k for k in all_columns.keys()]

        return cls(
            cls.__create_key,
            engine._pool,
            embedding_service,
            table_name,
            schema_name=schema_name,
            content_column=content_column,
            embedding_column=embedding_column,
            metadata_columns=metadata_columns,
            id_column=id_column,
            metadata_json_column=metadata_json_column,
            distance_strategy=distance_strategy,
            k=k,
            fetch_k=fetch_k,
            lambda_mult=lambda_mult,
            index_query_options=index_query_options,
        )

    @property
    def embeddings(self) -> Embeddings:
        return self.embedding_service

    async def aadd_embeddings(
        self,
        texts: Iterable[str],
        embeddings: list[list[float]],
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list] = None,
        **kwargs: Any,
    ) -> list[str]:
        """Add data along with embeddings to the table.

        Raises:
            :class:`InvalidTextRepresentationError <asyncpg.exceptions.InvalidTextRepresentationError>`: if the `ids` data type does not match that of the `id_column`.
        """
        if not ids:
            ids = [str(uuid.uuid4()) for _ in texts]
        else:
            # This is done to fill in any missing ids
            ids = [id if id is not None else str(uuid.uuid4()) for id in ids]
        if not metadatas:
            metadatas = [{} for _ in texts]
        # Insert embeddings
        for id, content, embedding, metadata in zip(ids, texts, embeddings, metadatas):
            metadata_col_names = (
                ", " + ", ".join(f'"{col}"' for col in self.metadata_columns)
                if len(self.metadata_columns) > 0
                else ""
            )
            insert_stmt = f'INSERT INTO "{self.schema_name}"."{self.table_name}"("{self.id_column}", "{self.content_column}", "{self.embedding_column}"{metadata_col_names}'
            values = {
                "langchain_id": id,
                "content": content,
                "embedding": str([float(dimension) for dimension in embedding]),
            }
            values_stmt = "VALUES (:langchain_id, :content, :embedding"
            inline_embed_func = getattr(
                self.embedding_service, "embed_query_inline", None
            )
            if not embedding and callable(inline_embed_func):
                values_stmt = f"VALUES (:langchain_id, :content, {self.embedding_service.embed_query_inline(content)}"  # type: ignore

            # Add metadata
            extra = copy.deepcopy(metadata)
            for metadata_column in self.metadata_columns:
                if metadata_column in metadata:
                    values_stmt += f", :{metadata_column}"
                    values[metadata_column] = metadata[metadata_column]
                    del extra[metadata_column]
                else:
                    values_stmt += ",null"

            # Add JSON column and/or close statement
            insert_stmt += (
                f""", "{self.metadata_json_column}")"""
                if self.metadata_json_column
                else ")"
            )
            if self.metadata_json_column:
                values_stmt += ", :extra)"
                values["extra"] = json.dumps(extra)
            else:
                values_stmt += ")"

            upsert_stmt = f' ON CONFLICT ("{self.id_column}") DO UPDATE SET "{self.content_column}" = EXCLUDED."{self.content_column}", "{self.embedding_column}" = EXCLUDED."{self.embedding_column}"'

            if self.metadata_json_column:
                upsert_stmt += f', "{self.metadata_json_column}" = EXCLUDED."{self.metadata_json_column}"'

            for column in self.metadata_columns:
                upsert_stmt += f', "{column}" = EXCLUDED."{column}"'

            upsert_stmt += ";"

            query = insert_stmt + values_stmt + upsert_stmt
            async with self.engine.connect() as conn:
                await conn.execute(text(query), values)
                await conn.commit()

        return ids

    async def aadd_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list] = None,
        **kwargs: Any,
    ) -> list[str]:
        """Embed texts and add to the table.

        Raises:
            :class:`InvalidTextRepresentationError <asyncpg.exceptions.InvalidTextRepresentationError>`: if the `ids` data type does not match that of the `id_column`.
        """
        # Check for inline embedding query
        inline_embed_func = getattr(self.embedding_service, "embed_query_inline", None)
        if callable(inline_embed_func):
            embeddings: list[list[float]] = [[] for _ in list(texts)]
        else:
            embeddings = await self.embedding_service.aembed_documents(list(texts))

        ids = await self.aadd_embeddings(
            texts, embeddings, metadatas=metadatas, ids=ids, **kwargs
        )
        return ids

    async def aadd_documents(
        self,
        documents: list[Document],
        ids: Optional[list] = None,
        **kwargs: Any,
    ) -> list[str]:
        """Embed documents and add to the table.

        Raises:
            :class:`InvalidTextRepresentationError <asyncpg.exceptions.InvalidTextRepresentationError>`: if the `ids` data type does not match that of the `id_column`.
        """
        texts = [doc.page_content for doc in documents]
        metadatas = [doc.metadata for doc in documents]
        if not ids:
            ids = [doc.id for doc in documents]
        ids = await self.aadd_texts(texts, metadatas=metadatas, ids=ids, **kwargs)
        return ids

    def _encode_image(self, uri: str) -> str:
        """Get base64 string from a image URI."""
        gcs_uri = re.match("gs://(.*?)/(.*)", uri)
        if gcs_uri:
            bucket_name, object_name = gcs_uri.groups()
            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(object_name)
            return base64.b64encode(blob.download_as_bytes()).decode("utf-8")

        web_uri = re.match(r"^(https?://).*", uri)
        if web_uri:
            response = requests.get(uri, stream=True)
            response.raise_for_status()
            return base64.b64encode(response.content).decode("utf-8")

        with open(uri, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")

    async def aadd_images(
        self,
        uris: list[str],
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list[str]] = None,
        store_uri_only: bool = False,
        **kwargs: Any,
    ) -> list[str]:
        """Embed images and add to the table.

        Args:
            uris (list[str]): List of image URIs to add to the table.
            metadatas (Optional[list[dict]]): List of metadatas to add to table records.
            ids: (Optional[list[str]]): List of IDs to add to table records.
            store_uri_only (bool): If True, stores the URI in the content column
                                   instead of the base64 encoded image. Defaults to False.
            **kwargs: Any other arguments to pass to the embedding service.

        Returns:
            List of record IDs added.
        """
        if metadatas is None:
            # Ensure URI is always in metadata if not explicitly provided elsewhere
            metadatas = [{"image_uri": uri} for uri in uris]
        elif store_uri_only:
            # If storing URI only and metadatas are provided, ensure image_uri is present
            for i, m in enumerate(metadatas):
                if "image_uri" not in m:  # Add if not already provided by user
                    m["image_uri"] = uris[i]

        texts_for_content_column: list[str]
        if store_uri_only:
            texts_for_content_column = uris
        else:
            texts_for_content_column = [self._encode_image(uri) for uri in uris]

        # Embeddings are always generated from the actual image content via URIs
        embeddings = self._images_embedding_helper(uris)

        ids = await self.aadd_embeddings(
            texts_for_content_column, embeddings, metadatas=metadatas, ids=ids, **kwargs
        )
        return ids

    async def adelete(
        self,
        ids: Optional[list] = None,
        **kwargs: Any,
    ) -> Optional[bool]:
        """Delete records from the table.

        Raises:
            :class:`InvalidTextRepresentationError <asyncpg.exceptions.InvalidTextRepresentationError>`: if the `ids` data type does not match that of the `id_column`.
        """
        if not ids:
            return False

        id_list = ", ".join([f"'{id}'" for id in ids])
        query = f'DELETE FROM "{self.schema_name}"."{self.table_name}" WHERE {self.id_column} in ({id_list})'
        async with self.engine.connect() as conn:
            await conn.execute(text(query))
            await conn.commit()
        return True

    @classmethod
    async def afrom_texts(  # type: ignore[override]
        cls: type[AsyncAlloyDBVectorStore],
        texts: list[str],
        embedding: Embeddings,
        engine: AlloyDBEngine,
        table_name: str,
        schema_name: str = "public",
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list] = None,
        content_column: str = "content",
        embedding_column: str = "embedding",
        metadata_columns: list[str] = [],
        ignore_metadata_columns: Optional[list[str]] = None,
        id_column: str = "langchain_id",
        metadata_json_column: str = "langchain_metadata",
        distance_strategy: DistanceStrategy = DEFAULT_DISTANCE_STRATEGY,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        index_query_options: Optional[QueryOptions] = None,
        **kwargs: Any,
    ) -> AsyncAlloyDBVectorStore:
        """Create an AsyncAlloyDBVectorStore instance from texts.

        Args:
            texts (list[str]): Texts to add to the vector store.
            embedding (Embeddings): Text embedding model to use.
            engine (AlloyDBEngine): Connection pool engine for managing connections to AlloyDB database.
            table_name (str): Name of an existing table.
            metadatas (Optional[list[dict]]): List of metadatas to add to table records.
            ids: (Optional[list[str]]): List of IDs to add to table records.
            content_column (str): Column that represent a Document’s page_content. Defaults to "content".
            embedding_column (str): Column for embedding vectors. The embedding is generated from the document value. Defaults to "embedding".
            metadata_columns (list[str]): Column(s) that represent a document's metadata.
            ignore_metadata_columns (list[str]): Column(s) to ignore in pre-existing tables for a document's metadata. Can not be used with metadata_columns. Defaults to None.
            id_column (str): Column that represents the Document's id. Defaults to "langchain_id".
            metadata_json_column (str): Column to store metadata as JSON. Defaults to "langchain_metadata".
            distance_strategy (DistanceStrategy): Distance strategy to use for vector similarity search. Defaults to COSINE_DISTANCE.
            k (int): Number of Documents to return from search. Defaults to 4.
            fetch_k (int): Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult (float): Number between 0 and 1 that determines the degree of diversity among the results with 0 corresponding to maximum diversity and 1 to minimum diversity. Defaults to 0.5.
            index_query_options (QueryOptions): Index query option.

        Raises:
            :class:`InvalidTextRepresentationError <asyncpg.exceptions.InvalidTextRepresentationError>`: if the `ids` data type does not match that of the `id_column`.

        Returns:
            AsyncAlloyDBVectorStore
        """
        vs = await cls.create(
            engine,
            embedding,
            table_name,
            schema_name=schema_name,
            content_column=content_column,
            embedding_column=embedding_column,
            metadata_columns=metadata_columns,
            ignore_metadata_columns=ignore_metadata_columns,
            id_column=id_column,
            metadata_json_column=metadata_json_column,
            distance_strategy=distance_strategy,
            k=k,
            fetch_k=fetch_k,
            lambda_mult=lambda_mult,
            index_query_options=index_query_options,
        )
        await vs.aadd_texts(texts, metadatas=metadatas, ids=ids, **kwargs)
        return vs

    @classmethod
    async def afrom_documents(  # type: ignore[override]
        cls: type[AsyncAlloyDBVectorStore],
        documents: list[Document],
        embedding: Embeddings,
        engine: AlloyDBEngine,
        table_name: str,
        schema_name: str = "public",
        ids: Optional[list] = None,
        content_column: str = "content",
        embedding_column: str = "embedding",
        metadata_columns: list[str] = [],
        ignore_metadata_columns: Optional[list[str]] = None,
        id_column: str = "langchain_id",
        metadata_json_column: str = "langchain_metadata",
        distance_strategy: DistanceStrategy = DEFAULT_DISTANCE_STRATEGY,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        index_query_options: Optional[QueryOptions] = None,
        **kwargs: Any,
    ) -> AsyncAlloyDBVectorStore:
        """Create an AsyncAlloyDBVectorStore instance from documents.

        Args:
            documents (list[Document]): Documents to add to the vector store.
            embedding (Embeddings): Text embedding model to use.
            engine (AlloyDBEngine): Connection pool engine for managing connections to AlloyDB database.
            table_name (str): Name of an existing table.
            metadatas (Optional[list[dict]]): List of metadatas to add to table records.
            ids: (Optional[list[str]]): List of IDs to add to table records.
            content_column (str): Column that represent a Document’s page_content. Defaults to "content".
            embedding_column (str): Column for embedding vectors. The embedding is generated from the document value. Defaults to "embedding".
            metadata_columns (list[str]): Column(s) that represent a document's metadata.
            ignore_metadata_columns (list[str]): Column(s) to ignore in pre-existing tables for a document's metadata. Can not be used with metadata_columns. Defaults to None.
            id_column (str): Column that represents the Document's id. Defaults to "langchain_id".
            metadata_json_column (str): Column to store metadata as JSON. Defaults to "langchain_metadata".
            distance_strategy (DistanceStrategy): Distance strategy to use for vector similarity search. Defaults to COSINE_DISTANCE.
            k (int): Number of Documents to return from search. Defaults to 4.
            fetch_k (int): Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult (float): Number between 0 and 1 that determines the degree of diversity among the results with 0 corresponding to maximum diversity and 1 to minimum diversity. Defaults to 0.5.
            index_query_options (QueryOptions): Index query option.

        Raises:
            :class:`InvalidTextRepresentationError <asyncpg.exceptions.InvalidTextRepresentationError>`: if the `ids` data type does not match that of the `id_column`.

        Returns:
            AsyncAlloyDBVectorStore
        """

        vs = await cls.create(
            engine,
            embedding,
            table_name,
            schema_name=schema_name,
            content_column=content_column,
            embedding_column=embedding_column,
            metadata_columns=metadata_columns,
            ignore_metadata_columns=ignore_metadata_columns,
            id_column=id_column,
            metadata_json_column=metadata_json_column,
            distance_strategy=distance_strategy,
            k=k,
            fetch_k=fetch_k,
            lambda_mult=lambda_mult,
            index_query_options=index_query_options,
        )
        texts = [doc.page_content for doc in documents]
        metadatas = [doc.metadata for doc in documents]
        await vs.aadd_texts(texts, metadatas=metadatas, ids=ids, **kwargs)
        return vs

    async def __query_collection(
        self,
        embedding: list[float],
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> Sequence[RowMapping]:
        """Perform similarity search query on database."""
        k = k if k else self.k
        operator = self.distance_strategy.operator
        search_function = self.distance_strategy.search_function

        columns = self.metadata_columns + [
            self.id_column,
            self.content_column,
            self.embedding_column,
        ]
        if self.metadata_json_column:
            columns.append(self.metadata_json_column)

        column_names = ", ".join(f'"{col}"' for col in columns)

        if filter and isinstance(filter, dict):
            filter = self._create_filter_clause(filter)
        filter = f"WHERE {filter}" if filter else ""
        inline_embed_func = getattr(self.embedding_service, "embed_query_inline", None)
        if not embedding and callable(inline_embed_func) and "query" in kwargs:
            query_embedding = self.embedding_service.embed_query_inline(kwargs["query"])  # type: ignore
        else:
            query_embedding = f"'{[float(dimension) for dimension in embedding]}'"
        stmt = f'SELECT {column_names}, {search_function}({self.embedding_column}, {query_embedding}) as distance FROM "{self.schema_name}"."{self.table_name}" {filter} ORDER BY {self.embedding_column} {operator} {query_embedding} LIMIT {k};'
        if self.index_query_options:
            async with self.engine.connect() as conn:
                # Set each query option individually
                for query_option in self.index_query_options.to_parameter():
                    query_options_stmt = f"SET LOCAL {query_option};"
                    await conn.execute(text(query_options_stmt))
                result = await conn.execute(text(stmt))
                result_map = result.mappings()
                results = result_map.fetchall()
        else:
            async with self.engine.connect() as conn:
                result = await conn.execute(text(stmt))
                result_map = result.mappings()
                results = result_map.fetchall()
        return results

    async def asimilarity_search(
        self,
        query: str,
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        """Return docs selected by similarity search on query."""
        inline_embed_func = getattr(self.embedding_service, "embed_query_inline", None)
        embedding = (
            []
            if callable(inline_embed_func)
            else await self.embedding_service.aembed_query(text=query)
        )
        kwargs["query"] = query

        return await self.asimilarity_search_by_vector(
            embedding=embedding, k=k, filter=filter, **kwargs
        )

    def _images_embedding_helper(self, image_uris: list[str]) -> list[list[float]]:
        # check if either `embed_images()` or `embed_image()` API is supported by the embedding service used
        if hasattr(self.embedding_service, "embed_images"):
            try:
                embeddings = self.embedding_service.embed_images(image_uris)
            except Exception as e:
                raise Exception(
                    f"Make sure your selected embedding model supports list of image URIs as input. {str(e)}"
                )
        elif hasattr(self.embedding_service, "embed_image"):
            try:
                embeddings = self.embedding_service.embed_image(image_uris)
            except Exception as e:
                raise Exception(
                    f"Make sure your selected embedding model supports list of image URIs as input. {str(e)}"
                )
        else:
            raise ValueError(
                "Please use an embedding model that supports image embedding."
            )
        return embeddings

    async def asimilarity_search_image(
        self,
        image_uri: str,
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        """Return docs selected by similarity search on query."""
        embedding = self._images_embedding_helper([image_uri])[0]

        return await self.asimilarity_search_by_vector(
            embedding=embedding, k=k, filter=filter, **kwargs
        )

    def _select_relevance_score_fn(self) -> Callable[[float], float]:
        """Select a relevance function based on distance strategy."""
        # Calculate distance strategy provided in
        # vectorstore constructor
        if self.distance_strategy == DistanceStrategy.COSINE_DISTANCE:
            return self._cosine_relevance_score_fn
        if self.distance_strategy == DistanceStrategy.INNER_PRODUCT:
            return self._max_inner_product_relevance_score_fn
        elif self.distance_strategy == DistanceStrategy.EUCLIDEAN:
            return self._euclidean_relevance_score_fn

    async def asimilarity_search_with_score(
        self,
        query: str,
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        """Return docs and distance scores selected by similarity search on query."""
        inline_embed_func = getattr(self.embedding_service, "embed_query_inline", None)
        embedding = (
            []
            if callable(inline_embed_func)
            else await self.embedding_service.aembed_query(text=query)
        )
        kwargs["query"] = query

        docs = await self.asimilarity_search_with_score_by_vector(
            embedding=embedding, k=k, filter=filter, **kwargs
        )
        return docs

    async def asimilarity_search_by_vector(
        self,
        embedding: list[float],
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        """Return docs selected by vector similarity search."""
        docs_and_scores = await self.asimilarity_search_with_score_by_vector(
            embedding=embedding, k=k, filter=filter, **kwargs
        )

        return [doc for doc, _ in docs_and_scores]

    async def asimilarity_search_with_score_by_vector(
        self,
        embedding: list[float],
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        """Return docs and distance scores selected by vector similarity search."""
        results = await self.__query_collection(
            embedding=embedding, k=k, filter=filter, **kwargs
        )

        documents_with_scores = []
        for row in results:
            metadata = (
                row[self.metadata_json_column]
                if self.metadata_json_column and row[self.metadata_json_column]
                else {}
            )
            for col in self.metadata_columns:
                metadata[col] = row[col]
            documents_with_scores.append(
                (
                    Document(
                        page_content=row[self.content_column],
                        metadata=metadata,
                        id=str(row[self.id_column]),
                    ),
                    row["distance"],
                )
            )

        return documents_with_scores

    async def amax_marginal_relevance_search(
        self,
        query: str,
        k: Optional[int] = None,
        fetch_k: Optional[int] = None,
        lambda_mult: Optional[float] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        """Return docs selected using the maximal marginal relevance."""
        embedding = await self.embedding_service.aembed_query(text=query)

        return await self.amax_marginal_relevance_search_by_vector(
            embedding=embedding,
            k=k,
            fetch_k=fetch_k,
            lambda_mult=lambda_mult,
            filter=filter,
            **kwargs,
        )

    async def amax_marginal_relevance_search_by_vector(
        self,
        embedding: list[float],
        k: Optional[int] = None,
        fetch_k: Optional[int] = None,
        lambda_mult: Optional[float] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        """Return docs selected using the maximal marginal relevance."""
        docs_and_scores = (
            await self.amax_marginal_relevance_search_with_score_by_vector(
                embedding,
                k=k,
                fetch_k=fetch_k,
                lambda_mult=lambda_mult,
                filter=filter,
                **kwargs,
            )
        )

        return [result[0] for result in docs_and_scores]

    async def amax_marginal_relevance_search_with_score_by_vector(
        self,
        embedding: list[float],
        k: Optional[int] = None,
        fetch_k: Optional[int] = None,
        lambda_mult: Optional[float] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        """Return docs and distance scores selected using the maximal marginal relevance."""
        results = await self.__query_collection(
            embedding=embedding, k=fetch_k, filter=filter, **kwargs
        )

        k = k if k else self.k
        fetch_k = fetch_k if fetch_k else self.fetch_k
        lambda_mult = lambda_mult if lambda_mult else self.lambda_mult
        embedding_list = [json.loads(row[self.embedding_column]) for row in results]
        mmr_selected = utils.maximal_marginal_relevance(
            np.array(embedding, dtype=np.float32),
            embedding_list,
            k=k,
            lambda_mult=lambda_mult,
        )

        documents_with_scores = []
        for row in results:
            metadata = (
                row[self.metadata_json_column]
                if self.metadata_json_column and row[self.metadata_json_column]
                else {}
            )
            for col in self.metadata_columns:
                metadata[col] = row[col]
            documents_with_scores.append(
                (
                    Document(
                        page_content=row[self.content_column],
                        metadata=metadata,
                        id=str(row[self.id_column]),
                    ),
                    row["distance"],
                )
            )

        return [r for i, r in enumerate(documents_with_scores) if i in mmr_selected]

    async def set_maintenance_work_mem(self, num_leaves: int, vector_size: int) -> None:
        """Set database maintenance work memory (for ScaNN index creation)."""
        # Required index memory in MB
        buffer = 1
        index_memory_required = (
            round(50 * num_leaves * vector_size * 4 / 1024 / 1024) + buffer
        )  # Convert bytes to MB
        query = f"SET maintenance_work_mem TO '{index_memory_required} MB';"
        async with self.engine.connect() as conn:
            await conn.execute(text(query))
            await conn.commit()

    async def aapply_vector_index(
        self,
        index: BaseIndex,
        name: Optional[str] = None,
        concurrently: bool = False,
    ) -> None:
        """Create index in the vector store table."""
        if isinstance(index, ExactNearestNeighbor):
            await self.adrop_vector_index()
            return

        # if extension name is mentioned, create the extension
        if index.extension_name:
            async with self.engine.connect() as conn:
                await conn.execute(
                    text(f"CREATE EXTENSION IF NOT EXISTS {index.extension_name}")
                )
                await conn.commit()
        function = index.get_index_function()

        filter = f"WHERE ({index.partial_indexes})" if index.partial_indexes else ""
        params = "WITH " + index.index_options()
        if name is None:
            if index.name == None:
                index.name = self.table_name + DEFAULT_INDEX_NAME_SUFFIX
            name = index.name
        stmt = f"CREATE INDEX {'CONCURRENTLY' if concurrently else ''} {name} ON \"{self.schema_name}\".\"{self.table_name}\" USING {index.index_type} ({self.embedding_column} {function}) {params} {filter};"
        if concurrently:
            async with self.engine.connect() as conn:
                await conn.execute(text("COMMIT"))
                await conn.execute(text(stmt))
        else:
            async with self.engine.connect() as conn:
                await conn.execute(text(stmt))
                await conn.commit()

    async def areindex(self, index_name: Optional[str] = None) -> None:
        """Re-index the vector store table."""
        index_name = index_name or self.table_name + DEFAULT_INDEX_NAME_SUFFIX
        query = f"REINDEX INDEX {index_name};"
        async with self.engine.connect() as conn:
            await conn.execute(text(query))
            await conn.commit()

    async def adrop_vector_index(
        self,
        index_name: Optional[str] = None,
    ) -> None:
        """Drop the vector index."""
        index_name = index_name or self.table_name + DEFAULT_INDEX_NAME_SUFFIX
        query = f"DROP INDEX IF EXISTS {index_name};"
        async with self.engine.connect() as conn:
            await conn.execute(text(query))
            await conn.commit()

    async def is_valid_index(
        self,
        index_name: Optional[str] = None,
    ) -> bool:
        """Check if index exists in the table."""
        index_name = index_name or self.table_name + DEFAULT_INDEX_NAME_SUFFIX
        query = f"""
        SELECT tablename, indexname
        FROM pg_indexes
        WHERE tablename = '{self.table_name}' AND schemaname = '{self.schema_name}' AND indexname = '{index_name}';
        """
        async with self.engine.connect() as conn:
            result = await conn.execute(text(query))
            result_map = result.mappings()
            results = result_map.fetchall()
        return bool(len(results) == 1)

    async def aget_by_ids(self, ids: Sequence[str]) -> list[Document]:
        """Get documents by ids."""

        quoted_ids = [f"'{id_val}'" for id_val in ids]
        id_list_str = ", ".join(quoted_ids)

        columns = self.metadata_columns + [
            self.id_column,
            self.content_column,
        ]
        if self.metadata_json_column:
            columns.append(self.metadata_json_column)

        column_names = ", ".join(f'"{col}"' for col in columns)

        query = f'SELECT {column_names} FROM "{self.schema_name}"."{self.table_name}" WHERE "{self.id_column}" IN ({id_list_str});'

        async with self.engine.connect() as conn:
            result = await conn.execute(text(query))
            result_map = result.mappings()
            results = result_map.fetchall()

        documents = []
        for row in results:
            metadata = (
                row[self.metadata_json_column]
                if self.metadata_json_column and row[self.metadata_json_column]
                else {}
            )
            for col in self.metadata_columns:
                metadata[col] = row[col]
            documents.append(
                (
                    Document(
                        page_content=row[self.content_column],
                        metadata=metadata,
                        id=str(row[self.id_column]),
                    )
                )
            )

        return documents

    def _handle_field_filter(
        self,
        field: str,
        value: Any,
    ) -> str:
        """Create a filter for a specific field.

        Args:
            field: name of field
            value: value to filter
                If provided as is then this will be an equality filter
                If provided as a dictionary then this will be a filter, the key
                will be the operator and the value will be the value to filter by

        Returns:
            sql where query as a string
        """
        if not isinstance(field, str):
            raise ValueError(
                f"field should be a string but got: {type(field)} with value: {field}"
            )

        if field.startswith("$"):
            raise ValueError(
                f"Invalid filter condition. Expected a field but got an operator: "
                f"{field}"
            )

        # Allow [a-zA-Z0-9_], disallow $ for now until we support escape characters
        if not field.isidentifier():
            raise ValueError(
                f"Invalid field name: {field}. Expected a valid identifier."
            )

        if isinstance(value, dict):
            # This is a filter specification
            if len(value) != 1:
                raise ValueError(
                    "Invalid filter condition. Expected a value which "
                    "is a dictionary with a single key that corresponds to an operator "
                    f"but got a dictionary with {len(value)} keys. The first few "
                    f"keys are: {list(value.keys())[:3]}"
                )
            operator, filter_value = list(value.items())[0]
            # Verify that that operator is an operator
            if operator not in SUPPORTED_OPERATORS:
                raise ValueError(
                    f"Invalid operator: {operator}. "
                    f"Expected one of {SUPPORTED_OPERATORS}"
                )
        else:  # Then we assume an equality operator
            operator = "$eq"
            filter_value = value

        if operator in COMPARISONS_TO_NATIVE:
            # Then we implement an equality filter
            # native is trusted input
            if isinstance(filter_value, str):
                filter_value = f"'{filter_value}'"
            native = COMPARISONS_TO_NATIVE[operator]
            return f"({field} {native} {filter_value})"
        elif operator == "$between":
            # Use AND with two comparisons
            low, high = filter_value

            return f"({field} BETWEEN {low} AND {high})"
        elif operator in {"$in", "$nin", "$like", "$ilike"}:
            # We'll do force coercion to text
            if operator in {"$in", "$nin"}:
                for val in filter_value:
                    if not isinstance(val, (str, int, float)):
                        raise NotImplementedError(
                            f"Unsupported type: {type(val)} for value: {val}"
                        )

                    if isinstance(val, bool):  # b/c bool is an instance of int
                        raise NotImplementedError(
                            f"Unsupported type: {type(val)} for value: {val}"
                        )

            if operator in {"$in"}:
                values = str(tuple(val for val in filter_value))
                return f"({field} IN {values})"
            elif operator in {"$nin"}:
                values = str(tuple(val for val in filter_value))
                return f"({field} NOT IN {values})"
            elif operator in {"$like"}:
                return f"({field} LIKE '{filter_value}')"
            elif operator in {"$ilike"}:
                return f"({field} ILIKE '{filter_value}')"
            else:
                raise NotImplementedError()
        elif operator == "$exists":
            if not isinstance(filter_value, bool):
                raise ValueError(
                    "Expected a boolean value for $exists "
                    f"operator, but got: {filter_value}"
                )
            else:
                if filter_value:
                    return f"({field} IS NOT NULL)"
                else:
                    return f"({field} IS NULL)"
        else:
            raise NotImplementedError()

    def _create_filter_clause(self, filters: Any) -> str:
        """Create LangChain filter representation to matching SQL where clauses

        Args:
            filters: Dictionary of filters to apply to the query.

        Returns:
            String containing the sql where query.
        """

        if not isinstance(filters, dict):
            raise ValueError(
                f"Invalid type: Expected a dictionary but got type: {type(filters)}"
            )
        if len(filters) == 1:
            # The only operators allowed at the top level are $AND, $OR, and $NOT
            # First check if an operator or a field
            key, value = list(filters.items())[0]
            if key.startswith("$"):
                # Then it's an operator
                if key.lower() not in ["$and", "$or", "$not"]:
                    raise ValueError(
                        f"Invalid filter condition. Expected $and, $or or $not "
                        f"but got: {key}"
                    )
            else:
                # Then it's a field
                return self._handle_field_filter(key, filters[key])

            if key.lower() == "$and" or key.lower() == "$or":
                if not isinstance(value, list):
                    raise ValueError(
                        f"Expected a list, but got {type(value)} for value: {value}"
                    )
                op = key[1:].upper()  # Extract the operator
                filter_clause = [self._create_filter_clause(el) for el in value]
                if len(filter_clause) > 1:
                    return f"({f' {op} '.join(filter_clause)})"
                elif len(filter_clause) == 1:
                    return filter_clause[0]
                else:
                    raise ValueError(
                        "Invalid filter condition. Expected a dictionary "
                        "but got an empty dictionary"
                    )
            elif key.lower() == "$not":
                if isinstance(value, list):
                    not_conditions = [
                        self._create_filter_clause(item) for item in value
                    ]
                    not_stmts = [f"NOT {condition}" for condition in not_conditions]
                    return f"({' AND '.join(not_stmts)})"
                elif isinstance(value, dict):
                    not_ = self._create_filter_clause(value)
                    return f"(NOT {not_})"
                else:
                    raise ValueError(
                        f"Invalid filter condition. Expected a dictionary "
                        f"or a list but got: {type(value)}"
                    )
            else:
                raise ValueError(
                    f"Invalid filter condition. Expected $and, $or or $not "
                    f"but got: {key}"
                )
        elif len(filters) > 1:
            # Then all keys have to be fields (they cannot be operators)
            for key in filters.keys():
                if key.startswith("$"):
                    raise ValueError(
                        f"Invalid filter condition. Expected a field but got: {key}"
                    )
            # These should all be fields and combined using an $and operator
            and_ = [self._handle_field_filter(k, v) for k, v in filters.items()]
            if len(and_) > 1:
                return f"({' AND '.join(and_)})"
            elif len(and_) == 1:
                return and_[0]
            else:
                raise ValueError(
                    "Invalid filter condition. Expected a dictionary "
                    "but got an empty dictionary"
                )
        else:
            return ""

    def get_by_ids(self, ids: Sequence[str]) -> list[Document]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list] = None,
        **kwargs: Any,
    ) -> list[str]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def add_documents(
        self,
        documents: list[Document],
        ids: Optional[list] = None,
        **kwargs: Any,
    ) -> list[str]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def add_images(
        self,
        uris: list[str],
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> list[str]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def delete(
        self,
        ids: Optional[list] = None,
        **kwargs: Any,
    ) -> Optional[bool]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    @classmethod
    def from_texts(  # type: ignore[override]
        cls: type[AsyncAlloyDBVectorStore],
        texts: list[str],
        embedding: Embeddings,
        engine: AlloyDBEngine,
        table_name: str,
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list] = None,
        content_column: str = "content",
        embedding_column: str = "embedding",
        metadata_columns: list[str] = [],
        ignore_metadata_columns: Optional[list[str]] = None,
        id_column: str = "langchain_id",
        metadata_json_column: str = "langchain_metadata",
        **kwargs: Any,
    ) -> AsyncAlloyDBVectorStore:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    @classmethod
    def from_documents(  # type: ignore[override]
        cls: type[AsyncAlloyDBVectorStore],
        documents: list[Document],
        embedding: Embeddings,
        engine: AlloyDBEngine,
        table_name: str,
        ids: Optional[list] = None,
        content_column: str = "content",
        embedding_column: str = "embedding",
        metadata_columns: list[str] = [],
        ignore_metadata_columns: Optional[list[str]] = None,
        id_column: str = "langchain_id",
        metadata_json_column: str = "langchain_metadata",
        **kwargs: Any,
    ) -> AsyncAlloyDBVectorStore:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def similarity_search(
        self,
        query: str,
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def similarity_search_image(
        self,
        image_uri: str,
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def similarity_search_with_score(
        self,
        query: str,
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def similarity_search_by_vector(
        self,
        embedding: list[float],
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def similarity_search_with_score_by_vector(
        self,
        embedding: list[float],
        k: Optional[int] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def max_marginal_relevance_search(
        self,
        query: str,
        k: Optional[int] = None,
        fetch_k: Optional[int] = None,
        lambda_mult: Optional[float] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def max_marginal_relevance_search_by_vector(
        self,
        embedding: list[float],
        k: Optional[int] = None,
        fetch_k: Optional[int] = None,
        lambda_mult: Optional[float] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[Document]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )

    def max_marginal_relevance_search_with_score_by_vector(
        self,
        embedding: list[float],
        k: Optional[int] = None,
        fetch_k: Optional[int] = None,
        lambda_mult: Optional[float] = None,
        filter: Optional[dict] | Optional[str] = None,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        raise NotImplementedError(
            "Sync methods are not implemented for AsyncAlloyDBVectorStore. Use AlloyDBVectorStore interface instead."
        )
