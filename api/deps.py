from fastapi import Request


def get_mod(request: Request):
    return request.app.state.mod


def get_rag_store(request: Request):
    return request.app.state.rag_store


def get_pg_conn(request: Request):
    return request.app.state.pg_conn
