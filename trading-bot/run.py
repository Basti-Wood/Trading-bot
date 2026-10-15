"""Start one engine and one WSGI server. Do not use multiple WSGI workers."""
import atexit
import multiprocessing
from pathlib import Path
from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    from app.config import Config
    from app.store import Store
    from app.engine import Engine
    from app.portfolio import PortfolioEngine
    from app.web import create_app
    from app.lock import InstanceLock
    from waitress import serve
    config = Config.from_env()
    store = Store(config.data_dir)
    lock = InstanceLock(config.data_dir / "engine.lock")
    atexit.register(lock.close)
    engine = PortfolioEngine(config, store) if config.trading_mode == "both" else Engine(config, store)
    atexit.register(engine.shutdown)
    engine.start()
    print(f"Trading dashboard: http://{config.host}:{config.port} | {'PAPER' if config.paper else 'LIVE'} | {config.trading_mode} strategies | entries paused", flush=True)
    try:
        serve(create_app(config, store), host=config.host, port=config.port, threads=4)
    finally:
        engine.shutdown()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
