import argparse
import asyncio


def main():
    parser = argparse.ArgumentParser(description='Luma durable build platform service')
    parser.add_argument('command', choices=['serve', 'worker', 'migrate', 'executor'])
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8091)
    args = parser.parse_args()
    if args.command == 'worker':
        from .worker import main as worker
        asyncio.run(worker())
    elif args.command == 'executor':
        from .executor import main as executor
        executor()
    elif args.command == 'migrate':
        from .config import Settings
        from .store import Store
        store = Store(Settings.from_env())
        try:
            store.migrate()
        finally:
            store.close()
    else:
        import uvicorn
        uvicorn.run('luma.build_platform.api:create_app', factory=True, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
