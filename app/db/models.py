"""
DB 테이블 정의. (A 담당, EE-03)

docs/spec/db.md에 정의된 users / auth_sessions / conversations / chat_turns 테이블을
SQLModel 클래스로 정의한다.

SQLModel 클래스에 table=True를 지정하면 실제 데이터베이스 테이블과 연결된다.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    # 현재 시간을 UTC(협정 세계시) 기준으로 반환한다.
    # default_factory에 등록하여 객체가 생성될 때마다 현재 시간을 구한다.
    return datetime.now(UTC)


class User(SQLModel, table=True):
    # 이 클래스가 데이터베이스의 users 테이블과 연결됨을 지정한다.
    __tablename__ = "users"

    # 사용자 기본 키(PK).
    # int | None: 정수 또는 None을 허용한다.
    # default=None: 새 사용자를 만들 때 ID를 지정하지 않으면
    # DB가 자동 생성하도록 한다. (일반적인 자동 증가 정수 PK)
    id: int | None = Field(default=None, primary_key=True)

    # 이메일 주소.
    # unique=True: 같은 이메일이 중복 저장되지 않도록 UNIQUE 제약조건을 설정한다.
    email: str = Field(unique=True)

    # 비밀번호 원문이 아니라 비밀번호 해시값을 저장한다.
    # 별도 Field 설정이 없으므로 기본적으로 필수 문자열 컬럼이다.
    password_hash: str

    # 계정 생성 시각.
    # 객체를 생성할 때마다 utc_now()를 호출하여 생성 시각을 설정한다.
    created_at: datetime = Field(default_factory=utc_now)


class AuthSession(SQLModel, table=True):
    # 로그인 세션 정보를 저장하는 auth_sessions 테이블.
    __tablename__ = "auth_sessions"

    # 세션 테이블의 기본 키.
    # None으로 생성하면 DB가 정수 ID를 자동 생성하도록 한다.
    id: int | None = Field(default=None, primary_key=True)

    # 세션을 소유한 사용자 ID.
    # users.id를 참조하는 외래 키(FK)이다.
    # 어떤 사용자의 로그인 세션인지 연결한다.
    user_id: int = Field(foreign_key="users.id")

    # 세션 토큰의 해시값.
    # unique=True로 동일한 토큰 해시가 중복 저장되는 것을 방지한다.
    # 일반적으로 원본 세션 토큰 대신 해시값을 DB에 저장한다.
    token_hash: str = Field(unique=True)

    # CSRF 공격 방지를 위한 토큰.
    # 요청이 정당한 경로에서 발생했는지 검증할 때 사용한다.
    csrf_token: str

    # 세션 생성 시각.
    created_at: datetime = Field(default_factory=utc_now)

    # 세션 만료 시각.
    # 만료 시각은 세션을 생성하는 로직에서 지정해야 한다.
    expires_at: datetime


class Conversation(SQLModel, table=True):
    # 대화방의 제목과 소유자 등 대화방 정보를 저장한다.
    __tablename__ = "conversations"

    # 대화방 테이블의 추가 제약조건이나 인덱스가 있다면 이곳에 정의할 수 있다.
    # 현재는 별도의 __table_args__를 정의하지 않는다.

    # 대화방 기본 키.
    # UUID 타입을 사용하며, 별도로 ID를 지정하지 않으면 uuid4()로 생성한다.
    # UUID는 정수 자동 증가 ID와 달리 무작위 기반의 고유 식별자를 만든다.
    id: UUID = Field(default_factory=uuid4, primary_key=True)

    # 대화방 소유자 ID.
    # users.id를 참조하는 외래 키이다.
    # index=True: 사용자 ID를 이용해 대화방을 조회할 때 검색 성능을 돕는다.
    user_id: int = Field(foreign_key="users.id", index=True)

    # 대화방 제목.
    title: str

    # 대화방 생성 시각.
    created_at: datetime = Field(default_factory=utc_now)

    # 대화방의 마지막 수정 시각.
    # 새 대화나 제목 변경 등 갱신 시각을 관리하는 로직에서 업데이트해야 한다.
    updated_at: datetime = Field(default_factory=utc_now)


class ChatTurn(SQLModel, table=True):
    # 대화방 안에서 오간 질문·답변 한 건을 저장한다.
    # 처리 중인 요청이나 실패한 요청도 기록할 수 있도록 상태 관련 필드가 있다.
    __tablename__ = "chat_turns"

    # 테이블 수준의 제약조건과 인덱스를 정의한다.
    __table_args__ = (
        # UNIQUE 제약조건.
        # 같은 사용자(user_id)가 같은 요청 ID(client_request_id)를
        # 중복해서 저장하지 못하도록 한다.
        # 클라이언트가 동일 요청을 재전송할 때 중복 처리를 방지하는 데 활용한다.
        UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_chat_turns_user_client_request",
        ),

        # 복합 인덱스.
        # 대화방별로 생성 시각과 ID를 기준으로 대화 기록을 조회하거나
        # 정렬할 때 검색 성능을 돕는다.
        # 인덱스에 포함된 컬럼 순서는 실제 조회 조건과 정렬 방식에 영향을 준다.
        Index(
            "ix_chat_turns_conversation_created_id",
            "conversation_id",
            "created_at",
            "id",
        ),
    )

    # 대화 기록 기본 키.
    # 새 기록을 생성할 때 ID를 지정하지 않으면 DB가 정수 ID를 생성한다.
    id: int | None = Field(default=None, primary_key=True)

    # 이 대화 기록이 속한 대화방의 UUID.
    # conversations.id를 참조하는 외래 키이다.
    conversation_id: UUID = Field(foreign_key="conversations.id")

    # 이 대화 기록의 소유자 ID.
    # users.id를 참조한다.
    # 소유자별 조회와 접근 권한 확인에 활용할 수 있다.
    user_id: int = Field(foreign_key="users.id")

    # 클라이언트가 생성한 요청 식별자.
    # 동일 사용자에게 같은 요청이 중복 저장되는 것을 방지하는 데 사용한다.
    client_request_id: UUID

    # 서버가 요청을 추적하기 위한 식별자.
    # 로그와 요청 처리 흐름을 연결할 때 사용할 수 있다.
    request_id: str

    # 질문의 난이도나 단계 등을 나타내는 값.
    # 구체적인 허용 값은 애플리케이션 로직이나 스펙에서 정한다.
    level: str

    # 사용자가 보낸 질문 내용.
    question: str

    # 생성된 답변.
    # str | None이므로 아직 답변이 없을 때는 None을 허용한다.
    answer: str | None = None

    # 요청 처리 상태.
    # 예: pending(처리 중), completed(완료), failed(실패).
    # 실제 허용 값과 상태 전환은 애플리케이션 로직에서 관리한다.
    status: str

    # 처리 실패 시 오류 코드를 저장한다.
    # 성공했거나 오류 코드가 아직 없으면 None을 허용한다.
    error_code: str | None = None

    # 대화 기록 생성 시각.
    created_at: datetime = Field(default_factory=utc_now)

    # 답변 처리가 완료된 시각.
    # 처리 전이거나 완료되지 않았다면 None일 수 있다.
    completed_at: datetime | None = None
