"""DB 엔진과 세션 관리.

L이 기본 구조를 작성하고 이후 A가 담당한 모듈이다.
라우터는 SessionDep을 통해 DB 세션을 주입받는다.
예: def handler(session: SessionDep): ...
"""

# UTC 시간과 현재 시각을 사용하기 위한 모듈
# 제너레이터의 반환 타입을 명시하기 위한 타입
from collections.abc import Generator
from datetime import UTC, datetime

# DB 파일을 저장할 디렉터리 경로를 다루기 위한 모듈
from pathlib import Path

# 타입과 FastAPI 의존성 주입을 선언하기 위한 모듈
from typing import Annotated

# Depends: FastAPI의 의존성 주입 기능
# Request: 현재 HTTP 요청 객체
from fastapi import Depends, Request

# Engine: 데이터베이스 연결을 관리하는 엔진 타입
# event: DB 연결 시 실행할 이벤트를 등록하는 기능
from sqlalchemy import Engine, event

# Session: DB 작업 단위인 세션
# SQLModel: 테이블 모델의 메타데이터 관리
# create_engine: DB 엔진 생성
# select: SQL SELECT 쿼리 구성
from sqlmodel import Session, SQLModel, create_engine, select

# 인증 세션 모델과 대화 질문·답변 기록 모델
from app.db.models import AuthSession, ChatTurn


def create_db_engine(database_url: str) -> Engine:
    """DB 엔진을 생성하고 SQLite 연결 설정을 등록한다."""

    # 전달받은 URL을 이용해 DB 엔진을 생성한다.
    # check_same_thread=False는 SQLite 연결의 스레드 사용 제한을 해제한다.
    # 단, 이것만으로 여러 쓰기의 동시성 문제가 해결되는 것은 아니다.
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )

    # 이 엔진에서 새로운 DB 연결이 만들어질 때 실행할 이벤트를 등록한다.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
        # SQLite 연결에서 외래 키 제약조건과 잠금 대기 시간을 설정한다.
        cursor = dbapi_connection.cursor()
        try:
            # 외래 키 제약조건을 활성화한다.
            cursor.execute("PRAGMA foreign_keys=ON")

            # DB 잠금이 해제될 때까지 최대 5초 대기하도록 설정한다.
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            # 설정 작업이 끝나면 커서를 닫는다.
            cursor.close()

    # 설정이 등록된 DB 엔진을 반환한다.
    return engine


def init_db(engine: Engine) -> None:
    """앱 시작 시 테이블을 준비하고 이전 실행의 미완료 작업을 정리한다."""

    # 엔진 URL에서 데이터베이스 파일 경로를 가져온다.
    database = engine.url.database

    # 파일 기반 DB라면 DB 파일을 저장할 상위 디렉터리를 생성한다.
    # 디렉터리가 이미 존재하면 그대로 유지한다.
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    # 파일 기반 SQLite DB에서 WAL 모드를 활성화한다.
    # WAL은 읽기와 쓰기의 동시 접근을 개선하지만,
    # 여러 쓰기 작업이 동시에 실행되도록 보장하지는 않는다.
    if engine.dialect.name == "sqlite" and database and database != ":memory:":
        # 엔진에서 DB 연결을 가져온다.
        connection = engine.raw_connection()
        try:
            cursor = connection.cursor()
            try:
                # SQLite의 저널 모드를 WAL로 설정한다.
                cursor.execute("PRAGMA journal_mode=WAL")
                mode = cursor.fetchone()[0]

                # WAL 모드가 적용되지 않았다면 초기화를 중단한다.
                if mode.lower() != "wal":
                    raise RuntimeError(
                        f"SQLite WAL 설정 실패: {mode}"
                    )
            finally:
                # 저널 모드 설정에 사용한 커서를 닫는다.
                cursor.close()
        finally:
            # 사용한 DB 연결을 반환한다.
            connection.close()

    # import된 모델의 메타데이터를 기준으로 없는 테이블을 생성한다.
    # 기존 테이블의 컬럼이나 구조를 자동으로 변경하지는 않는다.
    SQLModel.metadata.create_all(engine)

    # DB 세션을 열고 초기화 작업이 끝나면 자동으로 닫는다.
    with Session(engine) as session:
        # 현재 시각을 UTC 기준으로 구한다.
        now = datetime.now(UTC)

        # 만료 시각이 현재 시각보다 이전이거나 같은 인증 세션을 조회한다.
        expired_sessions = session.exec(
            select(AuthSession).where(
                AuthSession.expires_at <= now
            )
        ).all()

        # 조회된 만료 인증 세션을 DB에서 삭제하도록 표시한다.
        for auth_session in expired_sessions:
            session.delete(auth_session)

        # 만료 세션이 있었을 때만 삭제 사항을 DB에 반영한다.
        if expired_sessions:
            session.commit()

        # 이전 실행에서 처리 중 상태로 남은 대화 기록을 조회한다.
        # 예: AI 응답을 기다리던 중 서버가 종료된 경우
        pending_turns = session.exec(
            select(ChatTurn).where(
                ChatTurn.status == "pending"
            )
        ).all()

        # 미완료 대화 기록을 중단 상태로 변경한다.
        # 서버가 종료된 요청을 자동으로 재실행하지는 않는다.
        for turn in pending_turns:
            turn.status = "interrupted"
            turn.completed_at = now

        # 변경된 대화 기록이 있을 때만 DB에 반영한다.
        # 조회한 객체는 이미 세션에서 관리되므로 변경 사항이 자동 추적된다.
        if pending_turns:
            session.commit()


# 라우터에서 DB 세션을 요청할 때 실행되는 의존성 함수
def get_session(request: Request) -> Generator[Session, None, None]:
    # 애플리케이션 상태에 저장된 DB 엔진으로 세션을 생성한다.
    with Session(request.app.state.engine) as session:
        # yield로 세션을 라우터에 전달한다.
        # 요청 처리가 끝나면 with 블록이 종료되면서 세션을 닫는다.
        yield session


# SessionDep은 라우터에서 DB 세션을 주입받기 위한 타입 별칭이다.
# FastAPI는 Depends(get_session)을 통해 세션을 생성하고 전달한다.
SessionDep = Annotated[Session, Depends(get_session)]
