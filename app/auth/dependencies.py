
"""
인증 의존성 모듈입니다. (A 담당, EE-08)

다른 영역(라우터 등)은 인증 로직을 직접 구현하지 않고
이 모듈에서 제공하는 의존성만 사용합니다.

예시:
    def handler(user: CurrentUserDep): ...
    # 로그인한 사용자만 접근할 수 있습니다.

    def handler(user: CurrentUserDep, _: CsrfDep): ...
    # 로그인 + CSRF 검증이 필요한 요청입니다.

    def page(user: OptionalUserDep): ...
    # 로그인하지 않았으면 user는 None입니다.
    # 실제 /login 이동은 페이지 처리 코드에서 구현해야 합니다.
"""

import secrets  # CSRF 토큰 비교 시 타이밍 공격을 방지하는 비교 함수 등을 제공
from dataclasses import dataclass  # 데이터를 담는 클래스를 간단하게 정의
from datetime import UTC, datetime  # UTC 시간대와 현재 시각을 사용
from hashlib import sha256  # 토큰을 SHA-256 해시값으로 변환
from typing import Annotated  # 타입 정보에 FastAPI 의존성 정보를 결합

from fastapi import Depends, Request  # 의존성 주입 기능과 HTTP 요청 객체
from sqlalchemy.exc import SQLAlchemyError  # SQLAlchemy에서 발생하는 DB 예외
from sqlmodel import select  # 데이터 조회 SQL문을 생성하는 함수

from app.core.errors import AppError, ErrorCode  # 프로젝트에서 사용하는 공통 예외와 오류 코드
from app.db.models import AuthSession, User  # 인증 세션 테이블 모델과 사용자 테이블 모델
from app.db.session import SessionDep  # DB 세션을 주입받기 위한 프로젝트 공통 타입

SESSION_COOKIE_NAME = "session"  # 브라우저가 인증 토큰을 저장할 쿠키 이름


@dataclass(frozen=True)  # 데이터 클래스 생성, 생성 후 필드 변경을 막음
class CurrentUser:  # 현재 로그인한 사용자 정보를 담는 클래스
    id: int  # 사용자 ID
    email: str  # 사용자 이메일 주소


def hash_token(token: str) -> str:  # 원본 토큰을 입력받아 해시 문자열을 반환하는 함수
    """세션 토큰을 SHA-256으로 해시한다."""
    return sha256(token.encode("utf-8")).hexdigest()
    # token 문자열을 UTF-8 바이트로 변환
    # SHA-256 해시 계산
    # hexdigest()로 16진수 문자열 반환
    # 원본 토큰 대신 DB에 저장된 해시와 비교할 때 사용


def _get_auth_session(  # 세션 쿠키에 해당하는 인증 세션을 조회하는 내부 함수
    request: Request,  # 현재 HTTP 요청 객체
    session: SessionDep,  # DB 작업에 사용할 세션
) -> AuthSession | None:  # 인증 세션 객체 또는 None을 반환
    """세션 쿠키로 인증 세션을 조회하고 유효성을 확인한다.

    쿠키가 없거나 세션이 존재하지 않거나 만료된 경우 None을 반환한다.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    # 요청 쿠키에서 이름이 "session"인 쿠키의 값을 가져옴
    # 쿠키가 없으면 None

    if not token:  # 토큰이 없거나 빈 문자열이면
        return None  # 로그인 세션을 찾을 수 없으므로 None 반환

    try:  # DB 조회 중 발생할 수 있는 오류를 처리
        auth_session = session.exec(  # SQL문을 실행
            select(AuthSession).where(  # AuthSession 테이블에서 조건에 맞는 행을 조회
                AuthSession.token_hash == hash_token(token)
                # 쿠키의 원본 토큰을 해시한 값과 DB의 token_hash가 같은 행을 찾음
            )
        ).first()  # 조회 결과 중 첫 번째 행을 가져옴, 없으면 None
    except SQLAlchemyError:  # DB 관련 예외가 발생하면
        session.rollback()  # 현재 DB 트랜잭션을 롤백
        raise AppError(  # 프로젝트 공통 예외를 발생시킴
            status_code=503,  # 서비스 이용 불가 HTTP 상태 코드
            code=ErrorCode.DB_ERROR,  # 프로젝트에서 정의한 DB 오류 코드
            message="데이터베이스 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
            # 사용자에게 전달할 오류 메시지
        ) from None  # 원래 예외의 표시를 억제

    if auth_session is None:  # 해당 토큰의 세션이 DB에 없으면
        return None  # 유효한 인증 세션이 없음

    if auth_session.expires_at <= datetime.now(UTC):
        # 세션 만료 시각이 현재 UTC 시각보다 이전이거나 같으면
        return None  # 만료된 세션이므로 인증 실패 처리

    request.state.auth_session = auth_session
    # 현재 요청의 state에 인증 세션을 저장
    # 같은 요청에서 실행되는 다른 의존성(require_csrf 등)이 사용할 수 있음

    return auth_session  # 유효한 인증 세션을 반환


def _get_session_user(  # 인증 세션에 연결된 사용자 정보를 조회하는 내부 함수
    auth_session: AuthSession,  # 앞에서 찾은 인증 세션
    session: SessionDep,  # DB 세션
) -> CurrentUser | None:  # 사용자 정보 또는 None을 반환
    """인증 세션에 연결된 사용자를 조회한다."""

    try:  # 사용자 조회 중 DB 오류를 처리
        user = session.get(User, auth_session.user_id)
        # User 테이블에서 기본 키가 auth_session.user_id인 사용자 조회
        # 일반적으로 user_id는 User.id를 가리킴
    except SQLAlchemyError:  # DB 조회 중 SQLAlchemy 오류가 발생하면
        session.rollback()  # 트랜잭션 롤백
        raise AppError(  # 공통 오류로 변환
            status_code=503,  # 서비스 이용 불가
            code=ErrorCode.DB_ERROR,  # DB 오류 코드
            message="데이터베이스 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
            # 사용자에게 전달할 메시지
        ) from None  # 원래 예외의 표시를 억제

    if user is None:  # 세션에 연결된 사용자가 DB에 존재하지 않으면
        return None  # 사용자 정보를 반환할 수 없음

    return CurrentUser(  # DB의 User 모델을 인증용 데이터 객체로 변환
        id=user.id,  # 사용자 ID 복사
        email=user.email,  # 사용자 이메일 복사
    )  # 비밀번호 등 다른 사용자 필드는 외부에 노출하지 않음


def get_current_user(  # 로그인이 반드시 필요한 요청에서 사용하는 함수
    request: Request,  # 현재 HTTP 요청
    session: SessionDep,  # DB 세션
) -> CurrentUser:  # 인증에 성공하면 CurrentUser를 반환
    """세션 쿠키로 로그인 사용자를 찾는다.

    없거나 만료된 세션이면 AppError(401, AUTH_REQUIRED)를 raise 한다.
    """
    auth_session = _get_auth_session(request, session)
    # 쿠키 토큰으로 인증 세션을 조회하고 만료 여부를 확인

    if auth_session is None:  # 세션이 없거나 만료되었다면
        raise AppError(  # 로그인 필요 오류를 발생
            status_code=401,  # 인증되지 않았음을 나타내는 HTTP 상태 코드
            code=ErrorCode.AUTH_REQUIRED,  # 프로젝트의 인증 필요 오류 코드
            message="로그인이 필요합니다.",  # 사용자에게 전달할 메시지
        )

    user = _get_session_user(  # 인증 세션에 연결된 실제 사용자 조회
        auth_session,  # 위에서 검증한 세션 전달
        session,  # DB 세션 전달
    )

    if user is None:  # 사용자 정보가 존재하지 않으면
        raise AppError(  # 인증 실패로 처리
            status_code=401,  # HTTP 401
            code=ErrorCode.AUTH_REQUIRED,  # 인증 필요 오류
            message="로그인이 필요합니다.",  # 오류 메시지
        )

    return user  # 인증 성공: 현재 사용자 정보 반환


CurrentUserDep = Annotated[
    CurrentUser,  # 의존성 실행 결과를 CurrentUser 타입으로 취급
    Depends(get_current_user),  # FastAPI가 get_current_user()를 실행해 결과를 주입
]


def get_optional_user(  # 로그인이 필수가 아닌 페이지에서 사용하는 함수
    request: Request,  # 현재 HTTP 요청
    session: SessionDep,  # DB 세션
) -> CurrentUser | None:  # 로그인 상태에 따라 사용자 또는 None을 반환
    """페이지용. 로그인했으면 사용자, 아니면 None을 돌려준다."""

    auth_session = _get_auth_session(request, session)
    # 세션 쿠키를 확인하고 유효한 인증 세션을 조회

    if auth_session is None:  # 쿠키가 없거나 세션이 없거나 만료되었다면
        return None  # 오류를 발생시키지 않고 None 반환

    return _get_session_user(  # 인증 세션에 연결된 사용자 조회
        auth_session,  # 확인한 인증 세션 전달
        session,  # DB 세션 전달
    )  # 사용자가 없으면 None, 있으면 CurrentUser 반환


OptionalUserDep = Annotated[
    CurrentUser | None,  # 결과는 CurrentUser 또는 None
    Depends(get_optional_user),  # FastAPI가 함수를 실행하고 결과를 주입
]


def require_csrf(  # 로그인 상태에서 CSRF 검증이 필요한 요청에 사용하는 함수
    request: Request,  # 현재 HTTP 요청
    user: CurrentUserDep,  # 먼저 로그인 여부를 검증하고 사용자 정보를 주입받음
) -> None:  # 검증만 수행하므로 반환값은 없음
    """로그인 후 상태 변경 요청의 X-CSRF-Token 헤더를 검증한다.

    실패하면 AppError(403, CSRF_REJECTED)를 raise 한다.
    """
    csrf_token = request.headers.get("X-CSRF-Token")
    # HTTP 요청 헤더에서 X-CSRF-Token 값을 가져옴
    # 클라이언트가 보낸 CSRF 토큰

    auth_session = getattr(
        request.state,  # 현재 요청의 state 객체
        "auth_session",  # 가져올 속성 이름
        None,  # 속성이 없으면 None 반환
    )  # _get_auth_session()이 저장한 인증 세션을 가져옴

    if (  # 아래 조건 중 하나라도 참이면 CSRF 검증 실패
        not csrf_token  # 헤더에 CSRF 토큰이 없거나 빈 값이거나
        or auth_session is None  # 요청에 인증 세션이 저장되지 않았거나
        or not secrets.compare_digest(  # 두 토큰이 안전하게 비교했을 때 일치하지 않으면
            csrf_token.encode(),  # 요청 헤더의 토큰을 바이트로 변환
            auth_session.csrf_token.encode(),  # DB 세션의 CSRF 토큰도 바이트로 변환
        )  # compare_digest()는 일반적인 문자열 비교보다 타이밍 공격에 안전함
    ):
        raise AppError(  # 검증 실패 예외 발생
            status_code=403,  # 접근은 이해했지만 허용하지 않음을 나타내는 상태 코드
            code=ErrorCode.CSRF_REJECTED,  # 프로젝트의 CSRF 검증 실패 코드
            message="CSRF 토큰이 유효하지 않습니다.",  # 사용자에게 전달할 메시지
        )


CsrfDep = Annotated[
    None,  # CSRF 검증은 사용자 데이터 대신 None을 반환
    Depends(require_csrf),  # FastAPI가 require_csrf()를 실행하도록 설정
]
