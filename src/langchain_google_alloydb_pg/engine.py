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
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Thread
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Dict,
    List,
    Optional,
    Sequence,
    Type,
    TypeVar,
    Union,
)

import aiohttp
import google.auth  # type: ignore
import google.auth.transport.requests  # type: ignore
from google.cloud.alloydb.connector import AsyncConnector, IPTypes, RefreshStrategy
from sqlalchemy import MetaData, RowMapping, Table, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .version import __version__

if TYPE_CHECKING:
    import asyncpg  # type: ignore
    import google.auth.credentials  # type: ignore

T = TypeVar("T")

USER_AGENT = "langchain-google-alloydb-pg-python/" + __version__


async def _get_iam_principal_email(
    credentials: google.auth.credentials.Credentials,
) -> str:
    """Get email address associated with current authenticated IAM principal.

    Email will be used for automatic IAM database authentication to AlloyDB.

    Args:
        credentials (google.auth.credentials.Credentials):
            The credentials object to use in finding the associated IAM
            principal email address.

    Returns:
        email (str):
            The email address associated with the current authenticated IAM
            principal.
    """
    # refresh credentials if they are not valid
    if not credentials.valid:
        request = google.auth.transport.requests.Request()
        credentials.refresh(request)
    if hasattr(credentials, "_service_account_email"):
        return credentials._service_account_email.replace(".gserviceaccount.com", "")
    # call OAuth2 api to get IAM principal email associated with OAuth2 token
    url = f"https://oauth2.googleapis.com/tokeninfo?access_token={credentials.token}"
    async with aiohttp.ClientSession() as client:
        response = await client.get(url, raise_for_status=True)
        response_json: Dict = await response.json()
        email = response_json.get("email")
    if email is None:
        raise ValueError(
            "Failed to automatically obtain authenticated IAM principal's "
            "email address using environment's ADC credentials!"
        )
    return email.replace(".gserviceaccount.com", "")


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool = True

    def __post_init__(self) -> None:
        """Check if initialization parameters are valid.

        Raises:
            ValueError: If Column name is not string.
            ValueError: If data_type is not type string.
        """

        if not isinstance(self.name, str):
            raise ValueError("Column name must be type string")
        if not isinstance(self.data_type, str):
            raise ValueError("Column data_type must be type string")


class AlloyDBEngine:
    """A class for managing connections to a AlloyDB database."""

    _connector: Optional[AsyncConnector] = None
    __create_key = object()

    def __init__(
        self,
        key: object,
        engine: AsyncEngine,
        loop: Optional[asyncio.AbstractEventLoop],
        thread: Optional[Thread],
    ) -> None:
        """AlloyDBEngine constructor.

        Args:
            key(object): Prevent direct constructor usage.
            engine(AsyncEngine): Async engine connection pool.
            loop (Optional[asyncio.AbstractEventLoop]): Async event loop used to create the engine.
            thread (Optional[Thread] = None): Thread used to create the engine async.

        Raises:
            Exception: If the constructor is called directly by the user.
        """

        if key != AlloyDBEngine.__create_key:
            raise Exception(
                "Only create class through 'create' or 'create_sync' methods!"
            )
        self._engine = engine
        self._loop = loop
        self._thread = thread

    @classmethod
    def from_instance(
        cls: Type[AlloyDBEngine],
        project_id: str,
        region: str,
        cluster: str,
        instance: str,
        database: str,
        user: Optional[str] = None,
        password: Optional[str] = None,
        ip_type: Union[str, IPTypes] = IPTypes.PUBLIC,
        iam_account_email: Optional[str] = None,
    ) -> AlloyDBEngine:
        """Create an AlloyDBEngine from an AlloyDB instance.

        Args:
            project_id (str): GCP project ID.
            region (str): Cloud AlloyDB instance region.
            cluster (str): Cloud AlloyDB cluster name.
            instance (str): Cloud AlloyDB instance name.
            database (str): Database name.
            user (Optional[str], optional): Cloud AlloyDB user name. Defaults to None.
            password (Optional[str], optional): Cloud AlloyDB user password. Defaults to None.
            ip_type (Union[str, IPTypes], optional): IP address type. Defaults to IPTypes.PUBLIC.
            iam_account_email (Optional[str], optional): IAM service account email. Defaults to None.

        Returns:
            AlloyDBEngine: A newly created AlloyDBEngine instance.
        """
        # Running a loop in a background thread allows us to support
        # async methods from non-async environments
        loop = asyncio.new_event_loop()
        thread = Thread(target=loop.run_forever, daemon=True)
        thread.start()
        coro = cls._create(
            project_id,
            region,
            cluster,
            instance,
            database,
            ip_type,
            user,
            password,
            loop=loop,
            thread=thread,
            iam_account_email=iam_account_email,
        )
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    @classmethod
    async def _create(
        cls: Type[AlloyDBEngine],
        project_id: str,
        region: str,
        cluster: str,
        instance: str,
        database: str,
        ip_type: Union[str, IPTypes],
        user: Optional[str] = None,
        password: Optional[str] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        thread: Optional[Thread] = None,
        iam_account_email: Optional[str] = None,
    ) -> AlloyDBEngine:
        """Create an AlloyDBEngine from an AlloyDB instance.

        Args:
            project_id (str): GCP project ID.
            region (str): Cloud AlloyDB instance region.
            cluster (str): Cloud AlloyDB cluster name.
            instance (str): Cloud AlloyDB instance name.
            database (str): Database name.
            ip_type (Union[str, IPTypes], optional): IP address type. Defaults to IPTypes.PUBLIC.
            user (Optional[str], optional): Cloud AlloyDB user name. Defaults to None.
            password (Optional[str], optional): Cloud AlloyDB user password. Defaults to None.
            loop (Optional[asyncio.AbstractEventLoop]): Async event loop used to create the engine.
            thread (Optional[Thread] = None): Thread used to create the engine async.
            iam_account_email (Optional[str], optional): IAM service account email.

        Raises:
            ValueError: Raises error if only one of 'user' or 'password' is specified.

        Returns:
            AlloyDBEngine: A newly created AlloyDBEngine instance.
        """
        # error if only one of user or password is set, must be both or neither
        if bool(user) ^ bool(password):
            raise ValueError(
                "Only one of 'user' or 'password' were specified. Either "
                "both should be specified to use basic user/password "
                "authentication or neither for IAM DB authentication."
            )

        if cls._connector is None:
            cls._connector = AsyncConnector(
                user_agent=USER_AGENT, refresh_strategy=RefreshStrategy.LAZY
            )

        # if user and password are given, use basic auth
        if user and password:
            enable_iam_auth = False
            db_user = user
        # otherwise use automatic IAM database authentication
        else:
            enable_iam_auth = True
            if iam_account_email:
                db_user = iam_account_email
            else:
                # get application default credentials
                credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/userinfo.email"]
                )
                db_user = await _get_iam_principal_email(credentials)

        # anonymous function to be used for SQLAlchemy 'creator' argument
        async def getconn() -> asyncpg.Connection:
            conn = await cls._connector.connect(  # type: ignore
                f"projects/{project_id}/locations/{region}/clusters/{cluster}/instances/{instance}",
                "asyncpg",
                user=db_user,
                password=password,
                db=database,
                enable_iam_auth=enable_iam_auth,
                ip_type=ip_type,
            )
            return conn

        engine = create_async_engine(
            "postgresql+asyncpg://",
            async_creator=getconn,
        )
        return cls(cls.__create_key, engine, loop, thread)

    @classmethod
    async def afrom_instance(
        cls: Type[AlloyDBEngine],
        project_id: str,
        region: str,
        cluster: str,
        instance: str,
        database: str,
        user: Optional[str] = None,
        password: Optional[str] = None,
        ip_type: Union[str, IPTypes] = IPTypes.PUBLIC,
        iam_account_email: Optional[str] = None,
    ) -> AlloyDBEngine:
        """Create an AlloyDBEngine from an AlloyDB instance.

        Args:
            project_id (str): GCP project ID.
            region (str): Cloud AlloyDB instance region.
            cluster (str): Cloud AlloyDB cluster name.
            instance (str): Cloud AlloyDB instance name.
            database (str): Cloud AlloyDB database name.
            user (Optional[str], optional): Cloud AlloyDB user name. Defaults to None.
            password (Optional[str], optional): Cloud AlloyDB user password. Defaults to None.
            ip_type (Union[str, IPTypes], optional): IP address type. Defaults to IPTypes.PUBLIC.
            iam_account_email (Optional[str], optional): IAM service account email. Defaults to None.

        Returns:
            AlloyDBEngine: A newly created AlloyDBEngine instance.
        """
        return await cls._create(
            project_id,
            region,
            cluster,
            instance,
            database,
            ip_type,
            user,
            password,
            iam_account_email=iam_account_email,
        )

    @classmethod
    def from_engine(cls: Type[AlloyDBEngine], engine: AsyncEngine) -> AlloyDBEngine:
        """Create an AlloyDBEngine instance from an AsyncEngine."""
        return cls(cls.__create_key, engine, None, None)

    async def _aexecute(self, query: str, params: Optional[dict] = None) -> None:
        """Execute a SQL query."""
        async with self._engine.connect() as conn:
            await conn.execute(text(query), params)
            await conn.commit()

    async def _aexecute_outside_tx(self, query: str) -> None:
        """Execute a SQL query."""
        async with self._engine.connect() as conn:
            await conn.execute(text("COMMIT"))
            await conn.execute(text(query))

    async def _afetch(
        self, query: str, params: Optional[dict] = None
    ) -> Sequence[RowMapping]:
        """Fetch results from a SQL query."""
        async with self._engine.connect() as conn:
            result = await conn.execute(text(query), params)
            result_map = result.mappings()
            result_fetch = result_map.fetchall()

        return result_fetch

    def _execute(self, query: str, params: Optional[dict] = None) -> None:
        """Execute a SQL query."""
        return self._run_as_sync(self._aexecute(query, params))

    def _fetch(self, query: str, params: Optional[dict] = None) -> Sequence[RowMapping]:
        """Fetch results from a SQL query."""
        return self._run_as_sync(self._afetch(query, params))

    def _run_as_sync(self, coro: Awaitable[T]) -> T:
        """Run an async coroutine synchronously"""
        if not self._loop:
            raise Exception("Engine was initialized async.")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def ainit_vectorstore_table(
        self,
        table_name: str,
        vector_size: int,
        content_column: str = "content",
        embedding_column: str = "embedding",
        metadata_columns: List[Column] = [],
        metadata_json_column: str = "langchain_metadata",
        id_column: str = "langchain_id",
        overwrite_existing: bool = False,
        store_metadata: bool = True,
    ) -> None:
        """
        Create a table for saving of vectors to be used with AlloyDB.
        If table already exists and overwrite flag is not set, a TABLE_ALREADY_EXISTS error is thrown.

        Args:
            table_name (str): The table name.
            vector_size (int): Vector size for the embedding model to be used.
            content_column (str): Name of the column to store document content.
                Default: "page_content".
            embedding_column (str) : Name of the column to store vector embeddings.
                Default: "embedding".
            metadata_columns (List[Column]): A list of Columns to create for custom
                metadata. Default: []. Optional.
            metadata_json_column (str): The column to store extra metadata in JSON format.
                Default: "langchain_metadata". Optional.
            id_column (str):  Name of the column to store ids.
                Default: "langchain_id". Optional,
            overwrite_existing (bool): Whether to drop the existing table before insertion.
                Default: False.
            store_metadata (bool): Whether to store metadata in a JSON column if not specified by `metadata_columns`.
                Default: True.
        Raises:
            :class:`DuplicateTableError <asyncpg.exceptions.DuplicateTableError>`: if table already exists.
        """
        await self._aexecute("CREATE EXTENSION IF NOT EXISTS vector")

        if overwrite_existing:
            await self._aexecute(f'DROP TABLE IF EXISTS "{table_name}"')

        query = f"""CREATE TABLE "{table_name}"(
            "{id_column}" UUID PRIMARY KEY,
            "{content_column}" TEXT NOT NULL,
            "{embedding_column}" vector({vector_size}) NOT NULL"""
        for column in metadata_columns:
            nullable = "NOT NULL" if not column.nullable else ""
            query += f',\n"{column.name}" {column.data_type} {nullable}'
        if store_metadata:
            query += f',\n"{metadata_json_column}" JSON'
        query += "\n);"

        await self._aexecute(query)

    def init_vectorstore_table(
        self,
        table_name: str,
        vector_size: int,
        content_column: str = "content",
        embedding_column: str = "embedding",
        metadata_columns: List[Column] = [],
        metadata_json_column: str = "langchain_metadata",
        id_column: str = "langchain_id",
        overwrite_existing: bool = False,
        store_metadata: bool = True,
    ) -> None:
        """
        Create a table for saving of vectors to be used with AlloyDB.
        If table already exists and overwrite flag is not set, a TABLE_ALREADY_EXISTS error is thrown.

        Args:
            table_name (str): The table name.
            vector_size (int): Vector size for the embedding model to be used.
            content_column (str): Name of the column to store document content.
                Default: "page_content".
            embedding_column (str) : Name of the column to store vector embeddings.
                Default: "embedding".
            metadata_columns (List[Column]): A list of Columns to create for custom
                metadata. Default: []. Optional.
            metadata_json_column (str): The column to store extra metadata in JSON format.
                Default: "langchain_metadata". Optional.
            id_column (str):  Name of the column to store ids.
                Default: "langchain_id". Optional,
            overwrite_existing (bool): Whether to drop the existing table before insertion.
                Default: False.
            store_metadata (bool): Whether to store metadata in a JSON column if not specified by `metadata_columns`.
                Default: True.
        Raises:
            :class:`DuplicateTableError <asyncpg.exceptions.DuplicateTableError>`: if table already exists.
        """
        return self._run_as_sync(
            self.ainit_vectorstore_table(
                table_name,
                vector_size,
                content_column,
                embedding_column,
                metadata_columns,
                metadata_json_column,
                id_column,
                overwrite_existing,
                store_metadata,
            )
        )

    async def ainit_chat_history_table(self, table_name: str) -> None:
        """
        Create an AlloyDB table to save chat history messages.

        Args:
            table_name (str): The table name to store chat history.

        Returns:
            None
        """
        create_table_query = f"""CREATE TABLE IF NOT EXISTS "{table_name}"(
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            data JSONB NOT NULL,
            type TEXT NOT NULL
        );"""
        await self._aexecute(create_table_query)

    def init_chat_history_table(self, table_name: str) -> None:
        """
        Create an AlloyDB table to save chat history messages.

        Args:
            table_name (str): The table name to store chat history.

        Returns:
            None
        """
        return self._run_as_sync(
            self.ainit_chat_history_table(
                table_name,
            )
        )

    async def ainit_document_table(
        self,
        table_name: str,
        content_column: str = "page_content",
        metadata_columns: List[Column] = [],
        metadata_json_column: str = "langchain_metadata",
        store_metadata: bool = True,
    ) -> None:
        """
        Create a table for saving of langchain documents.
        If table already exists, a DuplicateTableError error is thrown.

        Args:
            table_name (str): The PgSQL database table name.
            content_column (str): Name of the column to store document content.
                Default: "page_content".
            metadata_columns (List[Column]): A list of Columns
                to create for custom metadata. Optional.
            metadata_json_column (str): The column to store extra metadata in JSON format.
                Default: "langchain_metadata". Optional.
            store_metadata (bool): Whether to store extra metadata in a metadata column
                if not described in 'metadata' field list (Default: True).
        """
        query = f"""CREATE TABLE "{table_name}"(
            {content_column} TEXT NOT NULL
            """
        for column in metadata_columns:
            nullable = "NOT NULL" if not column.nullable else ""
            query += f',\n"{column.name}" {column.data_type} {nullable}'
        metadata_json_column = metadata_json_column or "langchain_metadata"
        if store_metadata:
            query += f',\n"{metadata_json_column}" JSON'
        query += "\n);"

        await self._aexecute(query)

    def init_document_table(
        self,
        table_name: str,
        content_column: str = "page_content",
        metadata_columns: List[Column] = [],
        metadata_json_column: str = "langchain_metadata",
        store_metadata: bool = True,
    ) -> None:
        """
        Create a table for saving of langchain documents.
        If table already exists, a DuplicateTableError error is thrown.

        Args:
            table_name (str): The PgSQL database table name.
            content_column (str): Name of the column to store document content.
                Default: "page_content".
            metadata_columns (List[Column]): A list of Columns
                to create for custom metadata. Optional.
            metadata_json_column (str): The column to store extra metadata in JSON format.
                Default: "langchain_metadata". Optional.
            store_metadata (bool): Whether to store extra metadata in a metadata column
                if not described in 'metadata' field list (Default: True).
        """
        return self._run_as_sync(
            self.ainit_document_table(
                table_name,
                content_column,
                metadata_columns,
                metadata_json_column,
                store_metadata,
            )
        )

    async def _aload_table_schema(
        self,
        table_name: str,
    ) -> Table:
        """
        Load table schema from existing table in PgSQL database.

        Returns:
            (sqlalchemy.Table): The loaded table.
        """
        metadata = MetaData()
        async with self._engine.connect() as conn:
            try:
                await conn.run_sync(metadata.reflect, only=[table_name])
            except InvalidRequestError as e:
                raise ValueError(f"Table, {table_name}, does not exist: " + str(e))

        table = Table(table_name, metadata)
        # Extract the schema information
        schema = []
        for column in table.columns:
            schema.append(
                {
                    "name": column.name,
                    "type": column.type.python_type,
                    "max_length": getattr(column.type, "length", None),
                    "nullable": not column.nullable,
                }
            )

        return metadata.tables[table_name]
