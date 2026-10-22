
"""회원가입·로그인 API. (A 담당, EE-07·EE-08)"""

import re  # 정규표현식으로 이메일 형식 등을 검증
import secrets  # 예측하기 어려운 보안용 난수와 토큰 생성
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta  # UTC 현재 시각, 시간 차이, 만료 시각 계산

from fastapi import APIRouter, Request, Response

# APIRouter: 관련 API 경로를 묶음
# Request: 클라이언트가 보낸 HTTP 요청 정보에 접근
# Response: 쿠키 설정 및 삭제 등 HTTP 응답을 조작
from pwdlib import PasswordHash  # 비밀번호를 안전하게 해시하고 검증하는 기능
from pydantic import BaseModel, Field  # 요청 데이터의 구조와 유효성 검증
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

# IntegrityError: UNIQUE 위반 등 DB 무결성 제약 조건 오류
# SQLAlchemyError: SQLAlchemy 관련 DB 오류의 상위 예외
from sqlmodel import select  # DB 조회 SQL문 생성

from app.auth.dependencies import (
    SESSION_COOKIE_NAME,  # 세션 쿠키 이름: "session"
    CsrfDep,  # 로그인 + CSRF 검증을 실행하는 의존성 별칭
    CurrentUserDep,  # 현재 로그인 사용자를 확인하는 의존성 별칭
    hash_token,  # 원본 세션 토큰을 SHA-256 해시로 변환하는 함수
)
from app.core.deps import SettingsDep  # 설정값을 주입받는 의존성 별칭
from app.core.errors import AppError, ErrorCode  # 프로젝트 공통 예외와 오류 코드
from app.db.models import AuthSession, User  # 인증 세션 모델과 사용자 모델
from app.db.session import SessionDep  # DB 세션을 주입받는 의존성 별칭

router = APIRouter(prefix="/api/auth", tags=["auth"])
# 인증 관련 API 라우터 생성
# prefix: 이 라우터의 모든 경로 앞에 /api/auth를 붙임
# tags: API 문서에서 인증 관련 엔드포인트를 묶어 표시
# 예: @router.post("/login") -> POST /api/auth/login

password_hash = PasswordHash.recommended()
# pwdlib가 권장하는 비밀번호 해싱 알고리즘을 사용하도록 설정
# 평문 비밀번호를 해시하고, 로그인 때 해시와 입력 비밀번호를 검증

DUMMY_PASSWORD_HASH = password_hash.hash("dummy-password")
# 존재하지 않는 이메일로 로그인할 때 사용할 가짜 비밀번호 해시
# 실제 사용자 조회 성공 여부와 무관하게 비밀번호 검증 작업을 수행해
# 계정 존재 여부에 따른 응답 시간 차이를 줄이기 위한 장치
# 이 해시는 실제 사용자 비밀번호나 계정 정보가 아님

# 비밀번호에 쓸 수 있는 글자:
# ASCII 32~126 범위의 공백, 영어 대소문자, 숫자, 기호
# 유니코드 한글 등 해당 범위 밖의 문자는 허용하지 않음
PASSWORD_PATTERN = r"^[\x20-\x7E]+$"


class SignupRequest(BaseModel):
    # 회원가입 요청 JSON의 구조와 입력 검증 규칙
    email: str = Field(max_length=100)
    # 이메일은 문자열이며 최대 100자

    password: str = Field(
        min_length=8,  # 비밀번호 최소 8자
        max_length=64,  # 비밀번호 최대 64자
        pattern=PASSWORD_PATTERN,  # 허용된 ASCII 문자만 사용
    )


class LoginRequest(BaseModel):
    # 로그인 요청 JSON의 구조와 입력 검증 규칙
    email: str = Field(max_length=100)
    # 이메일 최대 100자

    password: str = Field(
        min_length=8,  # 비밀번호 최소 8자
        max_length=64,  # 비밀번호 최대 64자
        pattern=PASSWORD_PATTERN,  # 허용된 ASCII 문자만 사용
    )


def _validate_origin(
    request: Request,  # 클라이언트가 보낸 HTTP 요청
    settings,  # 사이트 주소 등의 설정값
) -> None:  # 검증만 수행하며 반환값은 없음
    """요청 Origin이 허용된 사이트인지 확인한다.

    허용되지 않은 Origin이면 CSRF_REJECTED 오류를 발생시킨다.
    """
    origin = request.headers.get("origin")
    # 요청 헤더에서 Origin 값을 가져옴
    # 일반적으로 요청을 시작한 사이트의 출처를 나타냄

    if origin != settings.site_origin:
        # 요청 Origin과 서버 설정에 등록된 허용 출처가 다르면
        # Origin 헤더가 없는 경우도 조건에 걸림
        raise AppError(
            status_code=403,  # 접근 거부
            code=ErrorCode.CSRF_REJECTED,  # 요청 출처 검증 실패
            message="허용되지 않은 요청입니다.",  # 외부에 전달할 오류 메시지
        )


def _validate_email(email: str) -> str:
    """이메일 형식을 검증하고 정규화한다.

    앞뒤 공백을 제거하고 소문자로 변환한다.
    """
    email = email.strip().lower()
    # strip(): 이메일 앞뒤의 공백 제거
    # lower(): 영문 대문자를 소문자로 변환
    # 예: " Test@Example.COM " -> "test@example.com"

    if not email:  # 공백 제거 후 이메일이 비어 있다면
        raise AppError(
            status_code=422,  # 입력값 검증 실패
            code=ErrorCode.VALIDATION_ERROR,  # 유효하지 않은 입력
            message="이메일을 입력해 주세요.",
        )

    if not re.fullmatch(
        r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+",
        email,
    ):
        # 이메일 전체가 정규표현식에 맞는지 검사
        # @ 앞뒤에 내용이 있어야 하고, 도메인에는 점이 있어야 함
        # 이메일의 모든 규칙을 완벽하게 검증하는 것은 아님
        raise AppError(
            status_code=422,  # 입력값 검증 실패
            code=ErrorCode.VALIDATION_ERROR,
            message="올바른 이메일 형식을 입력해 주세요.",
        )

    return email  # 검증 및 정규화가 끝난 이메일 반환


LOGIN_LIMIT = 5
LOGIN_WINDOW_SECONDS = 60

_login_attempts: dict[str, deque[float]] = defaultdict(deque)


def check_login_rate_limit(email: str) -> None:
    """이메일별 로그인 요청을 1분에 최대 5회로 제한한다."""
    now = time.monotonic()
    attempts = _login_attempts[email]

    # 60초가 지난 요청 기록 제거
    while attempts and now - attempts[0] >= LOGIN_WINDOW_SECONDS:
        attempts.popleft()

    # 제한 횟수에 도달했다면 요청 거부
    if len(attempts) >= LOGIN_LIMIT:
        raise AppError(
            status_code=429,
            code=ErrorCode.RATE_LIMIT_EXCEEDED,
            message="로그인 요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        )

    # 현재 로그인 요청 기록
    attempts.append(now)


@router.post("/signup", status_code=201)
# POST /api/auth/signup
# 회원가입 성공 시 HTTP 201 Created 응답
def signup(
    request: SignupRequest,  # 요청 본문을 검증한 회원가입 데이터
    http_request: Request,  # HTTP 요청 헤더 등의 정보
    session: SessionDep,  # DB 세션
    settings: SettingsDep,  # 사이트 설정 등을 담은 설정 객체
):
    """회원가입을 처리하고 새로운 사용자를 생성한다.

    이메일 중복과 입력값을 검증한 뒤 비밀번호를 해시하여 저장한다.
    """

    _validate_origin(
        http_request,
        settings,
    )
    # 요청을 보낸 사이트의 출처가 허용되는지 확인

    email = _validate_email(request.email)
    # 이메일을 검증하고 앞뒤 공백 제거 및 소문자 정규화

    try:  # DB에서 기존 이메일을 조회하는 과정의 오류 처리
        existing_user = session.exec(
            select(User).where(User.email == email)
            # User 테이블에서 정규화된 이메일과 일치하는 사용자 조회
        ).first()  # 첫 번째 결과를 반환, 없으면 None
    except SQLAlchemyError:  # DB 조회 자체가 실패하면
        session.rollback()  # 현재 DB 트랜잭션 정리
        raise AppError(
            status_code=503,  # DB 오류로 서비스 처리 불가
            code=ErrorCode.DB_ERROR,
            message="데이터베이스 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
        ) from None  # 원래 예외의 일반적인 예외 체인 표시 억제

    if existing_user is not None:  # 같은 이메일을 사용하는 계정이 있다면
        raise AppError(
            status_code=409,  # 기존 데이터와 충돌
            code=ErrorCode.ACCOUNT_EXISTS,
            message="이미 가입된 이메일이에요.",
        )

    user = User(
        email=email,  # 정규화된 이메일을 사용자 모델에 저장
        password_hash=password_hash.hash(
            request.password
            # 평문 비밀번호를 해시하여 저장할 값으로 변환
            # DB에는 원본 비밀번호가 아닌 해시값을 저장
        ),
    )

    session.add(user)
    # 새 사용자 객체를 DB 세션에 추가
    # 이 시점에는 보통 INSERT가 확정된 것은 아님

    try:
        session.commit()
        # 트랜잭션을 커밋해 사용자 생성 작업을 확정
        session.refresh(user)
        # DB에서 생성된 실제 값을 다시 읽어 user 객체 갱신
        # 예: DB가 자동 생성한 사용자 ID 확인
    except IntegrityError:
        # UNIQUE 제약 위반 등 무결성 오류
        # 사전 조회 후 다른 요청이 같은 이메일을 등록하는 경쟁 상황도
        # DB의 UNIQUE 제약이 설정되어 있다면 여기서 처리할 수 있음
        session.rollback()  # 실패한 트랜잭션 롤백
        raise AppError(
            status_code=409,  # 계정 중복 등 충돌
            code=ErrorCode.ACCOUNT_EXISTS,
            message="이미 가입된 이메일이에요.",
        ) from None
    except SQLAlchemyError:
        # 무결성 오류 외의 DB 작업 오류
        session.rollback()
        raise AppError(
            status_code=503,
            code=ErrorCode.DB_ERROR,
            message="데이터베이스 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
        ) from None

    return {
        "id": user.id,  # 생성된 사용자의 ID
        "email": user.email,  # 생성된 사용자의 이메일
    }
    # 비밀번호 해시 등 민감한 정보는 응답에 포함하지 않음


@router.post("/login")
# POST /api/auth/login
def login(
    request: LoginRequest,  # 요청 본문을 검증한 로그인 데이터
    http_request: Request,  # Origin 등의 요청 헤더 확인
    response: Response,  # 응답에 세션 쿠키를 설정하기 위한 객체
    session: SessionDep,  # DB 세션
    settings: SettingsDep,  # 사이트 및 세션 만료 설정
):
    """이메일과 비밀번호를 검증하여 로그인 세션을 생성한다.

    세션 토큰과 CSRF 토큰을 발급하고 세션 쿠키를 설정한다.
    """
    _validate_origin(
        http_request,
        settings,
    )
    # 허용된 사이트에서 온 요청인지 확인

    email = _validate_email(request.email)
    # 이메일 정규화 및 형식 검증
    
    check_login_rate_limit(email)
    # 로그인 횟수제한

    try:
        user = session.exec(
            select(User).where(User.email == email)
            # 이메일이 일치하는 사용자 조회
        ).first()  # 첫 번째 결과, 없으면 None
    except SQLAlchemyError:
        # 사용자 조회 중 DB 오류 발생
        session.rollback()
        raise AppError(
            status_code=503,
            code=ErrorCode.DB_ERROR,
            message="데이터베이스 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
        ) from None

    password_hash_value = (
        user.password_hash
        if user is not None
        else DUMMY_PASSWORD_HASH
    )
    # 사용자가 존재하면 실제 저장된 비밀번호 해시를 사용
    # 사용자가 없으면 미리 만든 가짜 해시를 사용
    # 계정 존재 여부와 무관하게 비밀번호 검증 작업을 수행해
    # 이메일 존재 여부를 시간 차이로 추측하기 어렵게 함

    password_valid = password_hash.verify(
        request.password,  # 사용자가 입력한 평문 비밀번호
        password_hash_value,  # DB의 해시 또는 가짜 해시
    )
    # 입력 비밀번호가 저장된 해시와 일치하는지 검증
    # 해시를 원래 비밀번호로 복원하는 것이 아님

    if user is None or not password_valid:
        # 사용자가 없거나 비밀번호가 틀리면
        # 두 경우 모두 같은 오류 메시지를 사용
        raise AppError(
            status_code=401,  # 인증 실패
            code=ErrorCode.INVALID_CREDENTIALS,
            message="이메일 또는 비밀번호가 올바르지 않습니다.",
        )

    user_id = user.id  # 인증된 사용자 ID를 보관
    user_email = user.email  # 인증된 사용자 이메일을 보관

    session_token = secrets.token_urlsafe(32)
    # 세션 인증에 사용할 예측하기 어려운 무작위 토큰 생성
    # 32는 바이트 단위이며, 결과 문자열은 일반적으로 약 43자
    # 이 원본 토큰은 브라우저 쿠키로 전달됨

    csrf_token = secrets.token_urlsafe(32)
    # CSRF 검증에 사용할 별도의 무작위 토큰 생성
    # 세션 토큰과는 용도가 다름

    auth_session = AuthSession(
        user_id=user_id,  # 이 세션을 소유하는 사용자 ID
        token_hash=hash_token(session_token),
        # 원본 세션 토큰이 아닌 SHA-256 해시를 DB에 저장
        csrf_token=csrf_token,
        # CSRF 토큰을 저장해 나중에 요청 헤더의 값과 비교
        expires_at=(
            datetime.now(UTC)
            # 현재 UTC 시각
            + timedelta(
                seconds=settings.session_ttl_seconds
                # 설정된 세션 유효 시간(초)을 더함
            )
        ),
        # 세션 만료 시각을 저장
    )

    session.add(auth_session)
    # 새 인증 세션을 DB 세션에 추가

    try:
        session.commit()
        # 인증 세션을 DB에 저장하고 트랜잭션 확정
    except SQLAlchemyError:
        # 세션 저장 중 DB 오류 발생
        session.rollback()
        raise AppError(
            status_code=503,
            code=ErrorCode.DB_ERROR,
            message="데이터베이스 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
        ) from None

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        # 쿠키 이름은 "session"
        value=session_token,
        # 브라우저에 저장할 원본 세션 토큰
        httponly=True,
        # JavaScript에서 document.cookie로 읽지 못하게 함
        secure=settings.cookie_secure,
        # 설정에 따라 HTTPS 연결에서만 쿠키를 전송하도록 함
        samesite="lax",
        # 다른 사이트에서 발생하는 일부 교차 사이트 요청에
        # 쿠키가 전송되는 것을 제한
        max_age=settings.session_ttl_seconds,
        # 쿠키의 최대 유효 기간(초)
        path="/",
        # 사이트 전체 경로에서 쿠키 사용
    )
    # 응답 헤더에 Set-Cookie를 설정하여 브라우저가 쿠키를 저장하게 함
    # 쿠키에 원본 토큰이 저장되지만 DB에는 토큰의 해시가 저장됨

    return {
        "user": {
            "id": user_id,  # 로그인한 사용자의 ID
            "email": user_email,  # 로그인한 사용자의 이메일
        },
        "csrf_token": csrf_token,
        # 클라이언트가 이후 상태 변경 요청의 헤더에 넣을 CSRF 토큰
        # 예: X-CSRF-Token: <csrf_token>
    }


@router.get("/me")
# GET /api/auth/me
# 현재 로그인한 사용자 정보를 확인하는 API
def me(
    user: CurrentUserDep,  # 세션 쿠키로 로그인 사용자를 인증
    request: Request,  # 인증 세션이 저장된 request.state에 접근
):
    """현재 로그인한 사용자의 정보를 조회한다.

    인증된 세션의 사용자 정보와 CSRF 토큰을 반환한다.
    """

    auth_session = getattr(
        request.state,  # 현재 요청에서 사용하는 임시 상태 객체
        "auth_session",  # 인증 의존성이 저장한 세션 속성
        None,  # 속성이 없으면 None
    )

    if auth_session is None:
        # 인증 의존성이 정상적으로 실행되었다면 보통 세션이 있어야 함
        # 방어적으로 세션이 없는 경우 인증 실패 처리
        raise AppError(
            status_code=401,
            code=ErrorCode.AUTH_REQUIRED,
            message="로그인이 필요합니다.",
        )

    return {
        "user": {
            "id": user.id,  # 인증된 사용자 ID
            "email": user.email,  # 인증된 사용자 이메일
        },
        "csrf_token": auth_session.csrf_token,
        # 현재 세션에 연결된 CSRF 토큰 반환
        # 클라이언트가 상태 변경 요청을 보낼 때 사용
    }


@router.post("/logout", status_code=204)
# POST /api/auth/logout
# 로그아웃 성공 시 HTTP 204 No Content 응답
def logout(
    user: CurrentUserDep,  # 로그인한 사용자인지 먼저 확인
    _: CsrfDep,  # CSRF 토큰 검증을 실행, 반환값은 사용하지 않음
    request: Request,  # 현재 요청의 인증 세션을 가져오기 위해 사용
    response: Response,  # 브라우저의 세션 쿠키를 삭제하기 위해 사용
    session: SessionDep,  # DB 세션
    settings: SettingsDep,  # 쿠키 설정값
):
    """현재 로그인한 사용자의 세션을 종료한다.

    세션을 삭제하고 브라우저의 세션 쿠키를 제거한다.
    """

    auth_session = getattr(
        request.state,  # 현재 요청의 상태 객체
        "auth_session",  # 인증 의존성이 저장한 속성 이름
        None,  # 속성이 없으면 None
    )

    if auth_session is not None:  # 인증 세션이 존재하면
        session.delete(auth_session)
        # DB 세션 삭제를 예약
        # commit() 전까지 DB 변경이 확정된 것은 아님

        try:
            session.commit()
            # DB에서 인증 세션 삭제를 확정
            # 이후 동일한 세션 토큰으로 인증 세션을 찾을 수 없게 됨
        except SQLAlchemyError:
            # DB 세션 삭제 중 오류 발생
            session.rollback()  # 삭제 작업 롤백
            raise AppError(
                status_code=503,
                code=ErrorCode.DB_ERROR,
                message="데이터베이스 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
            ) from None

    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        # 삭제할 쿠키 이름
        httponly=True,
        # 쿠키의 관련 옵션
        secure=settings.cookie_secure,
        # 쿠키 설정 시 사용한 secure 옵션과 일치시킴
        samesite="lax",
        # 쿠키 설정 시 사용한 SameSite 옵션과 일치시킴
        path="/",
        # 쿠키를 설정할 때 사용한 경로와 일치시킴
    )
    # 응답을 받은 브라우저에서 세션 쿠키를 제거하도록 지시
    # DB 세션 삭제와 브라우저 쿠키 삭제는 서로 다른 작업

    return None
    # 204 No Content 응답에는 응답 본문이 없음
