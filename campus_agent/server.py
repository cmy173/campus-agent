"""Local server entry point; intentionally refuses external interfaces."""
import argparse
import uvicorn

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Campus Agent 本地 Web 工作台')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run('campus_agent.web:create_app', factory=True, host='127.0.0.1', port=args.port, workers=1, access_log=False)
