"""대화 생성·내 기록 조회 API. (A 담당, EE-11)"""

from uuid import UUID  # UUID 타입 가져오기

from fastapi import APIRouter, Query  # 라우터 및 쿼리 매개변수 기능

from app.auth.dependencies import CsrfDep, CurrentUserDep  # CSRF 검증 및 로그인 사용자 의존성
from app.conversations.repository import (  # 대화 조회 관련 함수 가져오기
    DEFAULT_CONVERSATION_TITLE,  # 기본 대화 제목
    get_conversation_turns_for_user,  # 사용자 대화 및 턴 조회
    list_chats_for_user,  # 사용자 채팅 기록 조회
    list_conversations_for_user,  # 사용자 대화 목록 조회
)
from app.conversations.repository import (  # 대화 생성 함수 가져오기
    create_conversation as create_conversation_record,  # 함수 이름에 별칭 지정
)
from app.db.session import SessionDep  # 데이터베이스 세션 의존성

router = APIRouter(prefix="/api", tags=["conversations"])  # API 공통 경로 및 태그 설정

# SQLite의 정수 범위를 넘는 offset으로 쿼리 실행이 실패하지 않도록 제한한다.
MAX_OFFSET = 1_000_000  # offset 최댓값


@router.post("/conversations", status_code=201)  # 대화 생성 API, 성공 시 201 반환
def create_conversation(  # 대화 생성 함수
    user: CurrentUserDep,  # 로그인 사용자 정보
    _csrf: CsrfDep,  # CSRF 검증 실행
    session: SessionDep,  # 데이터베이스 세션
):
    conversation = create_conversation_record(  # 새 대화 생성
        session=session,  # DB 세션 전달
        user_id=user.id,  # 대화 소유자 지정
        title=DEFAULT_CONVERSATION_TITLE,  # 기본 제목 지정
    )

    return {  # 생성된 대화 정보 반환
        "id": conversation.id,  # 대화 ID
        "title": conversation.title,  # 대화 제목
        "created_at": conversation.created_at,  # 생성 시각
    }


@router.get("/me/conversations")  # 내 대화 목록 조회 API
def list_my_conversations(  # 대화 목록 조회 함수
    user: CurrentUserDep,  # 로그인 사용자 정보
    session: SessionDep,  # 데이터베이스 세션
    limit: int = Query(default=20, ge=1, le=100),  # 조회 개수: 기본 20, 범위 1~100
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),  # 시작 위치: 기본 0, 최댓값 제한
):
    conversations, has_more = list_conversations_for_user(  # 대화 목록 및 추가 페이지 여부 조회
        session=session,  # DB 세션 전달
        user_id=user.id,  # 로그인 사용자 기준 조회
        limit=limit,  # 조회 개수 전달
        offset=offset,  # 시작 위치 전달
    )

    return {  # 조회 결과 반환
        "items": [  # 대화 목록 구성
            {
                "id": conversation.id,  # 대화 ID
                "title": conversation.title,  # 대화 제목
                "created_at": conversation.created_at,  # 생성 시각
                "updated_at": conversation.updated_at,  # 수정 시각
            }
            for conversation in conversations  # 각 대화를 응답 항목으로 변환
        ],
        "limit": limit,  # 요청한 조회 개수
        "offset": offset,  # 요청한 시작 위치
        "has_more": has_more,  # 다음 페이지 존재 여부
    }


@router.get("/conversations/{conversation_id}")  # 특정 대화 조회 API
def get_conversation(  # 대화 상세 조회 함수
    conversation_id: UUID,  # URL에서 전달받은 대화 ID
    user: CurrentUserDep,  # 로그인 사용자 정보
    session: SessionDep,  # 데이터베이스 세션
):
    conversation, turns = get_conversation_turns_for_user(  # 대화와 해당 턴 목록 조회
        session=session,  # DB 세션 전달
        conversation_id=conversation_id,  # 조회할 대화 ID
        user_id=user.id,  # 사용자 소유권 확인용 ID
    )

    return {  # 대화 및 턴 정보 반환
        "conversation": {  # 대화 기본 정보
            "id": conversation.id,  # 대화 ID
            "title": conversation.title,  # 대화 제목
            "created_at": conversation.created_at,  # 생성 시각
            "updated_at": conversation.updated_at,  # 수정 시각
        },
        "turns": [  # 대화에 포함된 턴 목록
            {
                "id": turn.id,  # 턴 ID
                "conversation_id": turn.conversation_id,  # 소속 대화 ID
                "level": turn.level,  # 턴 레벨
                "question": turn.question,  # 사용자 질문
                "answer": turn.answer,  # 답변 내용
                "status": turn.status,  # 처리 상태
                "error_code": turn.error_code,  # 오류 코드
                "created_at": turn.created_at,  # 생성 시각
            }
            for turn in turns  # 각 턴을 응답 항목으로 변환
        ],
    }


@router.get("/me/chats")  # 내 채팅 기록 조회 API
def list_my_chats(  # 채팅 기록 목록 조회 함수
    user: CurrentUserDep,  # 로그인 사용자 정보
    session: SessionDep,  # 데이터베이스 세션
    limit: int = Query(default=20, ge=1, le=100),  # 조회 개수: 기본 20, 범위 1~100
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),  # 시작 위치: 기본 0, 최댓값 제한
):
    chats, has_more = list_chats_for_user(  # 채팅 기록 및 추가 페이지 여부 조회
        session=session,  # DB 세션 전달
        user_id=user.id,  # 로그인 사용자 기준 조회
        limit=limit,  # 조회 개수 전달
        offset=offset,  # 시작 위치 전달
    )

    return {  # 조회 결과 반환
        "items": [  # 채팅 기록 목록 구성
            {
                "id": turn.id,  # 턴 ID
                "conversation_id": turn.conversation_id,  # 소속 대화 ID
                "level": turn.level,  # 턴 레벨
                "question": turn.question,  # 사용자 질문
                "answer": turn.answer,  # 답변 내용
                "status": turn.status,  # 처리 상태
                "error_code": turn.error_code,  # 오류 코드
                "created_at": turn.created_at,  # 생성 시각
            }
            for turn in chats  # 각 채팅 기록을 응답 항목으로 변환
        ],
        "limit": limit,  # 요청한 조회 개수
        "offset": offset,  # 요청한 시작 위치
        "has_more": has_more,  # 다음 페이지 존재 여부
    }
