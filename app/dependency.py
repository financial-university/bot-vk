from typing import AsyncContextManager, Callable

from aiomysql.sa import create_engine, SAConnection
from aiomisc_dependency import dependency

connection = Callable[[], AsyncContextManager[SAConnection]]


def config_dependency(config):
    @dependency
    async def db_write() -> connection:
        engine = await create_engine(
            maxsize=50,
            pool_recycle=config["db_connect_timeout"],
            host=config["db_host"],
            user=config["db_user"],
            password=config["db_pass"],
            db=config["db_database"],
            port=config["db_port"],
            autocommit=True,
            connect_timeout=config["db_connect_timeout"],
            echo=True if config["debug"] else False,
        )
        yield engine.acquire
        engine.close()
        await engine.wait_closed()
