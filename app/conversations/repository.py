"""대화 저장소. (A 담당, EE-11)"""  # 대화와 대화 턴의 DB 저장 및 조회를 담당하는 모듈

import logging  # 오류 로그를 기록하기 위한 모듈
from datetime import UTC, datetime  # UTC 기준 날짜와 시간을 처리
from uuid import UUID  # UUID 형식의 식별자 타입

from sqlalchemy.exc import SQLAlchemyError  # DB 작업에서 발생하는 예외
from sqlmodel import Session, select  # DB 세션과 조회문 생성 도구

from app.core.errors import AppError, ErrorCode  # 공통 오류 객체와 오류 코드
from app.core.logging import log_event  # 이벤트 로그 기록 함수
from app.db.models import ChatTurn, Conversation  # 대화 및 대화 턴 DB 모델

_DB_ERROR_MESSAGE = "데이터베이스 오류가 발생했어요. 잠시 후 다시 시도해 주세요."
    # DB 오류 시 사용자에게 보여 줄 메시지
DEFAULT_CONVERSATION_TITLE = "새 대화"  # 대화 생성 시 기본 제목
_CONVERSATION_TITLE_MAX_LENGTH = 30  # 자동 생성 제목의 최대 글자 수


def _title_from_question(question: str) -> str:  # 질문을 대화 제목으로 정리하는 함수
    title = " ".join(question.split())  # 연속된 공백과 줄바꿈을 공백 하나로 정리
    if len(title) > _CONVERSATION_TITLE_MAX_LENGTH:  # 제목이 최대 길이를 초과하는지 확인
        return f"{title[:_CONVERSATION_TITLE_MAX_LENGTH]}…"  # 앞의 30자와 말줄임표를 반환
    return title  # 길이 제한 이내라면 정리한 제목을 그대로 반환


def _db_error() -> AppError:  # DB 오류를 공통 애플리케이션 오류로 변환하는 함수
    return AppError(503, ErrorCode.DB_ERROR, _DB_ERROR_MESSAGE)
    # HTTP 503과 DB 오류 코드 및 메시지를 반환


def create_conversation(  # 새 대화를 생성하고 DB에 저장하는 함수
    session: Session,  # DB 작업에 사용할 세션
    user_id: int,  # 대화를 생성하는 사용자 ID
    title: str,  # 새 대화의 제목
) -> Conversation:  # 생성된 Conversation 객체를 반환
    conversation = Conversation(  # 새 대화 모델 객체 생성
        user_id=user_id,  # 대화 소유자의 사용자 ID
        title=title,  # 대화 제목
    )

    try:  # DB 작업 중 발생할 수 있는 오류 처리 시작
        session.add(conversation)  # 대화 객체를 저장 대상으로 등록
        session.commit()  # 변경 사항을 DB에 저장
        session.refresh(conversation)  # DB에 저장된 최신 값을 객체에 반영

        log_event(  # 저장 성공 이벤트를 로그에 기록
            "db_save_success",  # 로그 이벤트 이름
            user_id=user_id,  # 로그에 사용자 ID 포함
            entity="conversation",  # 처리한 데이터 종류
        )
    except SQLAlchemyError as exc:  # DB 오류가 발생한 경우
        session.rollback()  # 실패한 트랜잭션의 변경 사항을 취소

        log_event(  # 저장 실패 이벤트를 로그에 기록
            "db_save_failed",  # 로그 이벤트 이름
            level=logging.ERROR,  # 오류 수준으로 기록
            user_id=user_id,  # 로그에 사용자 ID 포함
            entity="conversation",  # 실패한 데이터 종류
            error_type=type(exc).__name__,  # 예외 클래스 이름 기록
        )

        raise _db_error() from exc  # 공통 DB 오류를 발생시키고 원래 예외를 원인으로 보존

    return conversation  # 저장된 대화 객체 반환


def get_conversation_for_user(  # 특정 사용자가 소유한 대화를 조회하는 함수
    session: Session,  # DB 세션
    conversation_id: UUID,  # 조회할 대화의 UUID
    user_id: int,  # 대화 소유자를 확인할 사용자 ID
) -> Conversation | None:  # 대화가 있으면 반환하고 없으면 None 반환
    try:  # 조회 중 발생할 수 있는 DB 오류 처리
        return session.exec(  # 조회문을 실행
            select(Conversation).where(  # Conversation 테이블에서 조건에 맞는 대화 검색
                Conversation.id == conversation_id,  # 대화 ID가 요청한 ID와 일치해야 함
                Conversation.user_id == user_id,  # 해당 사용자의 대화만 조회
            )
        ).first()  # 첫 번째 결과 반환, 결과가 없으면 None 반환
    except SQLAlchemyError:  # DB 조회 중 오류가 발생한 경우
        session.rollback()  # 세션의 트랜잭션을 롤백

        raise _db_error() from None  # 내부 예외 상세를 숨기고 공통 DB 오류 발생


def create_pending_turn(  # 답변 생성 전 상태인 대화 턴을 생성하는 함수
    session: Session,  # DB 세션
    conversation_id: UUID,  # 턴이 속할 대화의 ID
    user_id: int,  # 요청한 사용자 ID
    client_request_id: UUID,  # 클라이언트 요청 식별자
    request_id: str,  # 서버 측 요청 추적 ID
    level: str,  # 요청에 사용된 처리 수준
    question: str,  # 사용자가 입력한 질문
) -> ChatTurn:  # 생성된 ChatTurn 객체를 반환
    conversation = get_conversation_for_user(  # 사용자의 대화인지 먼저 확인
        session,  # DB 세션 전달
        conversation_id,  # 확인할 대화 ID 전달
        user_id,  # 소유자 사용자 ID 전달
    )

    if conversation is None:  # 대화가 없거나 해당 사용자의 대화가 아닌 경우
        raise AppError(  # 애플리케이션 오류 발생
            404,  # HTTP 404: 찾을 수 없음
            ErrorCode.CONVERSATION_NOT_FOUND,  # 대화를 찾을 수 없다는 오류 코드
            "대화를 찾을 수 없어요.",  # 사용자에게 표시할 오류 메시지
        )

    turn = ChatTurn(  # 질문 정보를 담을 대화 턴 객체 생성
        conversation_id=conversation_id,  # 이 턴이 속한 대화 ID
        user_id=user_id,  # 턴을 소유한 사용자 ID
        client_request_id=client_request_id,  # 클라이언트 요청 식별자
        request_id=request_id,  # 서버 요청 추적 ID
        level=level,  # 요청에 사용된 처리 수준
        question=question,  # 사용자의 질문 원문
        status="pending",  # 답변 처리가 아직 진행 중임을 표시
    )
    try:  # 턴과 대화 정보를 DB에 저장하는 작업 시작
        has_existing_turn = session.exec(  # 기존 대화 턴이 있는지 조회
            select(ChatTurn.id)  # 전체 객체 대신 턴 ID만 조회
            .where(
                ChatTurn.conversation_id == conversation_id,  # 해당 대화에 속한 턴만 검색
                ChatTurn.user_id == user_id,  # 해당 사용자 소유의 턴만 검색
            )
            .limit(1)  # 존재 여부만 필요하므로 최대 한 건만 조회
        ).first()  # 결과가 있으면 ID 반환, 없으면 None 반환
        if (  # 대화 제목을 자동으로 설정할 조건 확인
            conversation.title == DEFAULT_CONVERSATION_TITLE  # 현재 제목이 기본 제목인지 확인
            and has_existing_turn is None  # 아직 기존 턴이 없는지 확인
        ):
            title = _title_from_question(question)  # 첫 질문을 바탕으로 대화 제목 생성
            if title:  # 생성된 제목이 비어 있지 않은 경우
                conversation.title = title  # 대화 제목을 질문 기반 제목으로 변경
        conversation.updated_at = datetime.now(UTC)  # 대화가 갱신된 시각을 UTC로 기록

        session.add(turn)  # 새 대화 턴을 저장 대상으로 등록
        session.add(conversation)  # 제목과 갱신 시각이 바뀐 대화도 저장 대상으로 등록
        session.commit()  # 턴과 대화의 변경 사항을 DB에 저장
        session.refresh(turn)  # 저장된 턴의 최신 값을 객체에 반영

        log_event(  # 턴 저장 성공 로그 기록
            "db_save_success",  # 로그 이벤트 이름
            user_id=user_id,  # 사용자 ID
            entity="chat_turn",  # 처리한 데이터 종류
            status="pending",  # 저장 당시 턴 상태
        )
    except SQLAlchemyError as exc:  # DB 저장 중 오류가 발생한 경우
        session.rollback()  # 실패한 트랜잭션의 변경 사항을 취소

        log_event(  # 저장 실패 로그 기록
            "db_save_failed",  # 로그 이벤트 이름
            level=logging.ERROR,  # 오류 수준으로 기록
            user_id=user_id,  # 사용자 ID
            entity="chat_turn",  # 실패한 데이터 종류
            status="pending",  # 실패 당시 턴 상태
            error_type=type(exc).__name__,  # 예외 클래스 이름
        )

        raise _db_error() from exc  # 공통 DB 오류 발생 및 원래 예외 보존

    return turn  # 생성 및 저장된 대화 턴 반환


def complete_turn(  # 대화 턴에 답변을 저장하고 완료 상태로 변경하는 함수
    session: Session,  # DB 세션
    turn_id: int,  # 완료 처리할 턴의 ID
    user_id: int,  # 요청한 사용자 ID
    answer: str,  # 생성된 답변 내용
) -> ChatTurn:  # 완료 처리된 ChatTurn 객체 반환
    try:  # 턴 조회 및 완료 처리 시작
        turn = session.exec(  # 조건에 맞는 턴 조회
            select(ChatTurn).where(  # ChatTurn 테이블에서 조건 검색
                ChatTurn.id == turn_id,  # 턴 ID가 일치해야 함
                ChatTurn.user_id == user_id,  # 해당 사용자 소유의 턴이어야 함
            )
        ).first()  # 첫 번째 결과 반환, 없으면 None 반환

        if turn is None:  # 대상 턴을 찾지 못한 경우
            raise AppError(  # 애플리케이션 오류 발생
                404,  # HTTP 404: 찾을 수 없음
                ErrorCode.CONVERSATION_NOT_FOUND,  # 대화 관련 오류 코드
                "대화를 찾을 수 없어요.",  # 사용자용 오류 메시지
            )

        turn.answer = answer  # 생성된 답변 내용을 턴에 저장
        turn.status = "completed"  # 턴 상태를 완료로 변경
        turn.completed_at = datetime.now(UTC)  # 완료된 시각을 UTC로 기록

        session.add(turn)  # 변경된 턴을 저장 대상으로 등록
        session.commit()  # 변경 사항을 DB에 저장
        session.refresh(turn)  # DB의 최신 값을 객체에 반영

        log_event(  # 턴 완료 성공 로그 기록
            "db_save_success",  # 로그 이벤트 이름
            user_id=user_id,  # 사용자 ID
            entity="chat_turn",  # 처리한 데이터 종류
            status="completed",  # 완료된 턴 상태
        )
    except AppError:  # 대상 턴이 없는 등 앱 오류가 발생한 경우
        session.rollback()  # 현재 트랜잭션 롤백
        raise  # 기존 AppError를 호출자에게 그대로 전달
    except SQLAlchemyError as exc:  # DB 작업 중 오류가 발생한 경우
        session.rollback()  # 실패한 트랜잭션의 변경 사항을 취소

        log_event(  # 완료 처리 실패 로그 기록
            "db_save_failed",  # 로그 이벤트 이름
            level=logging.ERROR,  # 오류 수준으로 기록
            user_id=user_id,  # 사용자 ID
            entity="chat_turn",  # 실패한 데이터 종류
            status="completed",  # 처리하려던 상태
            error_type=type(exc).__name__,  # 예외 클래스 이름
        )

        raise _db_error() from exc  # 공통 DB 오류 발생 및 원래 예외 보존

    return turn  # 완료 처리된 턴 반환


def fail_turn(  # 대화 턴을 실패 상태로 변경하고 오류 코드를 저장하는 함수
    session: Session,  # DB 세션
    turn_id: int,  # 실패 처리할 턴의 ID
    user_id: int,  # 요청한 사용자 ID
    error_code: str,  # 실패 원인을 나타내는 오류 코드
) -> ChatTurn:  # 실패 처리된 ChatTurn 객체 반환
    try:  # 턴 조회 및 실패 처리 시작
        turn = session.exec(  # 조건에 맞는 턴 조회
            select(ChatTurn).where(  # ChatTurn 테이블에서 조건 검색
                ChatTurn.id == turn_id,  # 턴 ID가 일치해야 함
                ChatTurn.user_id == user_id,  # 해당 사용자 소유의 턴이어야 함
            )
        ).first()  # 첫 번째 결과 반환, 없으면 None 반환

        if turn is None:  # 대상 턴을 찾지 못한 경우
            raise AppError(  # 애플리케이션 오류 발생
                404,  # HTTP 404: 찾을 수 없음
                ErrorCode.CONVERSATION_NOT_FOUND,  # 대화 관련 오류 코드
                "대화를 찾을 수 없어요.",  # 사용자용 오류 메시지
            )

        turn.status = "failed"  # 턴 상태를 실패로 변경
        turn.error_code = error_code  # 실패 원인을 나타내는 오류 코드 저장
        turn.completed_at = datetime.now(UTC)  # 실패 처리가 완료된 시각을 UTC로 기록

        session.add(turn)  # 변경된 턴을 저장 대상으로 등록
        session.commit()  # 실패 상태와 오류 코드를 DB에 저장
        session.refresh(turn)  # DB의 최신 값을 객체에 반영

        log_event(  # 턴 실패 처리 성공 로그 기록
            "db_save_success",  # DB 저장 성공 이벤트 이름
            user_id=user_id,  # 사용자 ID
            entity="chat_turn",  # 처리한 데이터 종류
            status="failed",  # 실패 상태로 변경했음을 기록
        )
    except AppError:  # 대상 턴이 없는 등 앱 오류가 발생한 경우
        session.rollback()  # 현재 트랜잭션 롤백
        raise  # 기존 AppError를 호출자에게 그대로 전달
    except SQLAlchemyError as exc:  # DB 작업 중 오류가 발생한 경우
        session.rollback()  # 실패한 트랜잭션의 변경 사항을 취소

        log_event(  # 실패 상태 저장 중 발생한 DB 오류 기록
            "db_save_failed",  # 로그 이벤트 이름
            level=logging.ERROR,  # 오류 수준으로 기록
            user_id=user_id,  # 사용자 ID
            entity="chat_turn",  # 실패한 데이터 종류
            status="failed",  # 처리하려던 상태
            error_type=type(exc).__name__,  # 예외 클래스 이름
        )

        raise _db_error() from exc  # 공통 DB 오류 발생 및 원래 예외 보존

    return turn  # 실패 상태로 저장된 턴 반환


def get_recent_completed_turns(  # 특정 대화의 최근 완료된 턴을 조회하는 함수
    session: Session,  # DB 세션
    conversation_id: UUID,  # 조회할 대화 ID
    user_id: int,  # 대화 소유자의 사용자 ID
    limit: int,  # 가져올 최대 턴 개수
) -> list[ChatTurn]:  # 완료된 턴 목록을 반환
    conversation = get_conversation_for_user(  # 사용자가 소유한 대화인지 확인
        session,  # DB 세션 전달
        conversation_id,  # 대화 ID 전달
        user_id,  # 사용자 ID 전달
    )

    if conversation is None:  # 대화가 없거나 사용자의 소유가 아닌 경우
        raise AppError(  # 애플리케이션 오류 발생
            404,  # HTTP 404: 찾을 수 없음
            ErrorCode.CONVERSATION_NOT_FOUND,  # 대화 관련 오류 코드
            "대화를 찾을 수 없어요.",  # 사용자용 오류 메시지
        )

    try:  # 완료된 턴 조회 시작
        return list(  # 조회 결과를 리스트로 변환해 반환
            session.exec(  # SQL 조회문 실행
                select(ChatTurn)  # ChatTurn 모델의 데이터 조회
                .where(
                    ChatTurn.conversation_id == conversation_id,  # 해당 대화의 턴만 조회
                    ChatTurn.user_id == user_id,  # 해당 사용자 소유의 턴만 조회
                    ChatTurn.status == "completed",  # 완료된 턴만 조회
                )
                .order_by(
                    ChatTurn.created_at.desc(),  # 생성 시각이 최신인 순서로 정렬
                    ChatTurn.id.desc(),  # 생성 시각이 같으면 ID 내림차순 정렬
                )
                .limit(limit)  # 반환할 최대 턴 개수 제한
            ).all()  # 조건에 맞는 모든 결과 가져오기
        )
    except SQLAlchemyError:  # DB 조회 중 오류가 발생한 경우
        session.rollback()  # 세션의 트랜잭션 롤백

        raise _db_error() from None  # 내부 예외 상세를 숨기고 공통 DB 오류 발생


def list_conversations_for_user(  # 사용자의 대화 목록을 페이지 단위로 조회하는 함수
    session: Session,  # DB 세션
    user_id: int,  # 조회할 사용자의 ID
    limit: int,  # 한 페이지에 반환할 최대 대화 개수
    offset: int,  # 앞에서 건너뛸 대화 개수
) -> tuple[list[Conversation], bool]:  # 대화 목록과 다음 페이지 존재 여부 반환
    try:  # 대화 목록 조회 시작
        conversations = list(  # 조회 결과를 리스트로 변환
            session.exec(  # SQL 조회문 실행
                select(Conversation)  # Conversation 모델의 데이터 조회
                .where(Conversation.user_id == user_id)  # 해당 사용자 소유의 대화만 조회
                .order_by(
                    Conversation.updated_at.desc(),  # 최근에 갱신된 대화부터 정렬
                    Conversation.id.desc(),  # 갱신 시각이 같으면 ID 내림차순 정렬
                )
                .offset(offset)  # 페이지네이션을 위해 앞의 항목을 건너뜀
                .limit(limit + 1)  # 다음 페이지 존재 여부 확인을 위해 한 건 더 조회
            ).all()  # 조건에 맞는 모든 결과 가져오기
        )

        has_more = len(conversations) > limit  # 요청한 개수보다 많으면 다음 페이지가 있음

        return conversations[:limit], has_more  # 요청한 개수만 반환하고 다음 페이지 여부도 전달

    except SQLAlchemyError:  # DB 조회 중 오류가 발생한 경우
        session.rollback()  # 세션의 트랜잭션 롤백

        raise _db_error() from None  # 내부 예외 상세를 숨기고 공통 DB 오류 발생


def get_conversation_turns_for_user(  # 대화 하나와 그 대화에 속한 모든 턴을 조회하는 함수
    session: Session,  # DB 세션
    conversation_id: UUID,  # 조회할 대화 ID
    user_id: int,  # 대화 소유자의 사용자 ID
) -> tuple[Conversation, list[ChatTurn]]:  # 대화 객체와 턴 목록을 함께 반환
    conversation = get_conversation_for_user(  # 대화가 존재하고 사용자의 소유인지 확인
        session,  # DB 세션 전달
        conversation_id,  # 대화 ID 전달
        user_id,  # 사용자 ID 전달
    )

    if conversation is None:  # 대화가 없거나 사용자의 소유가 아닌 경우
        raise AppError(  # 애플리케이션 오류 발생
            404,  # HTTP 404: 찾을 수 없음
            ErrorCode.CONVERSATION_NOT_FOUND,  # 대화 관련 오류 코드
            "대화를 찾을 수 없어요.",  # 사용자용 오류 메시지
        )

    try:  # 대화에 속한 턴 목록 조회 시작
        turns = list(  # 조회 결과를 리스트로 변환
            session.exec(  # SQL 조회문 실행
                select(ChatTurn)  # ChatTurn 모델의 데이터 조회
                .where(
                    ChatTurn.conversation_id == conversation_id,  # 해당 대화에 속한 턴만 조회
                    ChatTurn.user_id == user_id,  # 해당 사용자 소유의 턴만 조회
                )
                .order_by(
                    ChatTurn.created_at.asc(),  # 오래된 턴부터 시간순으로 정렬
                    ChatTurn.id.asc(),  # 생성 시각이 같으면 ID 오름차순 정렬
                )
            ).all()  # 조건에 맞는 모든 턴 가져오기
        )

        return conversation, turns  # 대화 객체와 턴 목록을 함께 반환

    except SQLAlchemyError:  # DB 조회 중 오류가 발생한 경우
        session.rollback()  # 세션의 트랜잭션 롤백

        raise _db_error() from None  # 내부 예외 상세를 숨기고 공통 DB 오류 발생


def list_chats_for_user(  # 사용자의 전체 대화 턴을 페이지 단위로 조회하는 함수
    session: Session,  # DB 세션
    user_id: int,  # 조회할 사용자의 ID
    limit: int,  # 한 페이지에 반환할 최대 턴 개수
    offset: int,  # 앞에서 건너뛸 턴 개수
) -> tuple[list[ChatTurn], bool]:  # 턴 목록과 다음 페이지 존재 여부 반환
    try:  # 전체 대화 턴 목록 조회 시작
        turns = list(  # 조회 결과를 리스트로 변환
            session.exec(  # SQL 조회문 실행
                select(ChatTurn)  # ChatTurn 모델의 데이터 조회
                .where(ChatTurn.user_id == user_id)  # 해당 사용자 소유의 턴만 조회
                .order_by(
                    ChatTurn.created_at.desc(),  # 생성 시각이 최신인 턴부터 정렬
                    ChatTurn.id.desc(),  # 생성 시각이 같으면 ID 내림차순 정렬
                )
                .offset(offset)  # 페이지네이션을 위해 앞의 항목을 건너뜀
                .limit(limit + 1)  # 다음 페이지 존재 여부 확인을 위해 한 건 더 조회
            ).all()  # 조건에 맞는 모든 결과 가져오기
        )

        has_more = len(turns) > limit  # 요청한 개수보다 많으면 다음 페이지가 있음

        return turns[:limit], has_more  # 요청한 개수만 반환하고 다음 페이지 여부도 전달

    except SQLAlchemyError:  # DB 조회 중 오류가 발생한 경우
        session.rollback()  # 세션의 트랜잭션 롤백

        raise _db_error() from None  # 내부 예외 상세를 숨기고 공통 DB 오류 발생
    