"""Owner Take-a-Message conversational transition and timing policy."""
from __future__ import annotations

import re
import time
from typing import Optional

from app.config import settings

TRANSITION_GRACE_SECONDS = 3.0

_NON_PERSON_OBJECTS = frozenset({
    "horário", "horario", "horários", "horarios",
    "estacionamento", "estacionamentos",
    "endereço", "endereco", "endereços", "enderecos",
    "peça", "peças", "peca", "pecas",
    "vaga", "vagas",
    "agenda", "agendas",
    "data", "datas",
    "sistema", "sistemas",
    "valor", "valores",
    "preço", "preços", "preco", "precos",
    "serviço", "serviços", "servico", "servicos",
    "sala", "salas",
    "produto", "produtos",
    "documento", "documentos",
    "português", "portugues",
    "inglês", "ingles",
    "você", "voce", "vocês", "voces",
})


def is_owner_availability_hold(text: str) -> bool:
    """Return True when Kevin has told the caller he is checking/trying owner availability."""
    if not isinstance(text, str):
        return False
    trimmed = text.strip()
    if not trimmed:
        return False

    normalized = f" {trimmed.lower()} "
    if any(
        neg in normalized
        for neg in (
            "not available",
            "unavailable",
            "não está disponível",
            "nao esta disponivel",
            "não está disponivel",
            "nao está disponível",
            "não disponível",
            "nao disponivel",
            "indisponível",
            "indisponivel",
            "não pode atender",
            "nao pode atender",
            "não pode falar",
            "nao pode falar",
            "não está livre",
            "nao esta livre",
            "não consigo falar",
            "nao consigo falar",
            "não conseguiu atender",
            "nao conseguiu atender",
            "não vou transferir",
            "nao vou transferir",
            "não vou passar",
            "nao vou passar",
            "não vou tentar",
            "nao vou tentar",
            "não vou verificar",
            "nao vou verificar",
            "não vou checar",
            "nao vou checar",
            "não vou",
            "nao vou",
            "não vamos",
            "nao vamos",
            "não posso",
            "nao posso",
        )
    ):
        return False

    if any(
        q in normalized
        for q in (
            " posso ", " podemos ", " devo ", " deveria ", " devemos ", " deveríamos ", " deveriamos ",
            " você quer ", " voce quer ", " quer que ",
            " gostaria de ", " prefere ",
            " can i ", " may i ", " should i ", " shall i ", " could i ",
            " would you like ", " do you want ", " would you prefer ", " do you prefer ",
            " can we ", " may we ", " should we ", " shall we ", " could we ",
        )
    ) or re.search(
        r"\b(?:can\s+i|may\s+i|should\s+i|shall\s+i|could\s+i|would\s+you\s+like|do\s+you\s+want|would\s+you\s+prefer|do\s+you\s+prefer|can\s+we|may\s+we|should\s+we|shall\s+we|could\s+we|posso|podemos|devo|deveria|devemos|dever[ií]amos|voc[eê]\s+quer|quer\s+que|gostaria\s+de|prefere)\b",
        normalized,
        re.IGNORECASE,
    ):
        return False

    # Trailing or explicit consent and conditional clauses reject even with committed hold prefix
    if re.search(
        r"\b(?:"
        r"(?:would|will|is|does)\s+(?:that|this|it)\s+(?:be\s+)?(?:okay|ok|all\s*right|alright)"
        r"|(?:would|will|does|is)\s+(?:that|this|it)\s+work\s+for\s+you"
        r"|if\s+(?:that|this|it)(?:['’]s|\s+is)\s+(?:okay|ok|fine|all\s*right|alright)"
        r"|if\s+you\s+(?:agree|don['’]t\s+mind|do\s+not\s+mind)"
        r"|se\s+(?:isso\s+)?estiver\s+tudo\s+bem"
        r"|se\s+voc[eê]\s+(?:concordar|(?:n[aã]o\s+)?se\s+importar)"
        r"|seria\s+tudo\s+bem(?:\s+para\s+voc[eê])?"
        r"|(?:isso\s+)?funciona\s+para\s+voc[eê]"
        r")\b",
        normalized,
        re.IGNORECASE,
    ):
        return False

    hold_markers = (
        "let me see if",
        "let me check if",
        "i'm going to try",
        "i will try",
        "i'll try",
        "let me try",
        "one moment",
        "please hold",
        "hold on",
    )
    owner_markers = (
        "available",
        "reach",
        "get ahold",
        "get a hold",
        "connect you",
        "transfer you",
        "try",
    )

    english_match = any(marker in normalized for marker in hold_markers) and any(
        marker in normalized for marker in owner_markers
    )
    if english_match:
        return True

    # Portuguese availability patterns: must match checking OWNER/PERSON availability or transferring
    # 1. Checking if person is available / free
    match1 = re.search(
        r"(?:deixe-?me|deixa eu|deixe eu|vou|vamos|aguarde|um momento|um instante|s[oó] um minuto|por favor aguarde)?\s*(?:ver|verificar|verifico|checar|checo|conferir|confiro|olhar|olho)\s+(?:se\s+)?(?:(?:o|a)\s+)?((?:[a-zA-ZÀ-ÿ]+\s+)*[a-zA-ZÀ-ÿ]+)?\s*(?:est[aá]|t[aá])\s+(?:dispon[ií]vel|livre)",
        normalized,
        re.IGNORECASE,
    )
    if match1:
        subj = (match1.group(1) or "").strip().lower()
        if not subj or not any(w in _NON_PERSON_OBJECTS for w in subj.split()):
            return True

    # 2. Checking if person can take the call / speak (not speaking a language)
    match2 = re.search(
        r"(?:deixe-?me|deixa eu|deixe eu|vou|vamos|aguarde|um momento|um instante|s[oó] um minuto|por favor aguarde)\s+(?:ver|verificar|verifico|checar|checo|conferir|confiro|olhar|olho)\s+(?:se\s+)?(?:(?:o|a)\s+)?((?:[a-zA-ZÀ-ÿ]+\s+)*[a-zA-ZÀ-ÿ]+)?\s*(?:pode|consegue)\s+(?:atender|falar(?!\s+(?:portugu[eê]s|ingl[eê]s|espanhol|franc[eê]s|alem[aã]o|italiano|idioma)))",
        normalized,
        re.IGNORECASE,
    )
    if match2:
        subj = (match2.group(1) or "").strip().lower()
        if not subj or not any(w in _NON_PERSON_OBJECTS for w in subj.split()):
            return True

    # 3. Checking availability of person (e.g. 'verifico a disponibilidade do Deli')
    match3 = re.search(
        r"(?:verificar|verifico|checar|checo|conferir|confiro)\s+a\s+disponibilidade\s+d[eoa]\s+([a-zA-ZÀ-ÿ]+)",
        normalized,
        re.IGNORECASE,
    )
    if match3:
        target = match3.group(1).strip().lower()
        if target not in _NON_PERSON_OBJECTS:
            return True

    # 4. Transferring / connecting call to person with affirmative hold prefix
    match4 = re.search(
        r"(?:vou\s+tentar|vamos\s+tentar|vou|vamos|aguarde|um instante|um momento|s[oó] um minuto|por favor aguarde)\s+(?:transferir|conectar|passar)\s+(?:a\s+liga[cç][aã]o|a\s+chamada|voc[eê])(?:\s+(?:para|com)\s+(?:o\s+|a\s+)?([a-zA-ZÀ-ÿ]+))?",
        normalized,
        re.IGNORECASE,
    )
    if match4:
        target = (match4.group(1) or "").strip().lower()
        if not target or target not in _NON_PERSON_OBJECTS:
            return True

    match4b = re.search(
        r"(?:vou\s+tentar|vamos\s+tentar|vou|vamos|aguarde|um instante|um momento|s[oó] um minuto|por favor aguarde)\s+(?:transferir|conectar|passar)\s+(?:para|com)\s+(?:o\s+|a\s+)?([a-zA-ZÀ-ÿ]+)",
        normalized,
        re.IGNORECASE,
    )
    if match4b:
        target = match4b.group(1).strip().lower()
        if target not in _NON_PERSON_OBJECTS:
            return True

    # 5. Trying to speak with person
    match5 = re.search(
        r"(?:vou\s+tentar|vamos\s+tentar|vou\s+ver\s+se\s+consigo)\s+falar\s+com\s+(?:o\s+|a\s+)?([a-zA-ZÀ-ÿ]+)",
        normalized,
        re.IGNORECASE,
    )
    if match5:
        target = match5.group(1).strip().lower()
        if target not in _NON_PERSON_OBJECTS:
            return True

    return False


# Backward-compatible alias
is_owner_availability_hold_text = is_owner_availability_hold


def should_apply_grace(action: str, hold_offered: bool) -> bool:
    """The 3s grace applies once for explicit owner decline when a hold offer was begun/completed."""
    return action == "decline" and bool(hold_offered)


def compute_grace_delay(
    action: str = "decline",
    hold_offered: bool = False,
    intent_accepted_at: Optional[float] = None,
    speaking_finished_at: Optional[float] = None,
    now: Optional[float] = None,
    **kwargs,
) -> float:
    """Calculate remaining grace delay in seconds using a monotonic clock.

    The 3s pause is anchored at the LATER of:
    - when the owner intent was first accepted for transition
    - when current speech/playout finishes

    Timeout actions (30s) always receive 0.0s grace delay.
    """
    if not should_apply_grace(action, hold_offered):
        return 0.0
    now = time.monotonic() if now is None else now
    accepted_at = intent_accepted_at if intent_accepted_at is not None and intent_accepted_at > 0 else now
    speaking_at = speaking_finished_at if speaking_finished_at is not None and speaking_finished_at > 0 else 0.0
    anchor = max(accepted_at, speaking_at)
    elapsed = max(0.0, now - anchor)
    return max(0.0, TRANSITION_GRACE_SECONDS - elapsed)


def build_unavailable_speech_text(
    owner_name: str = "",
    hold_offered: bool = False,
    language: Optional[str] = "en",
) -> str:
    """Spoken unavailability text for VoicePipeline."""
    owner = owner_name.strip() if isinstance(owner_name, str) and owner_name.strip() else settings.user_name
    from app.services.personal_language import is_portuguese
    if is_portuguese(language):
        if hold_offered:
            return f"Infelizmente, {owner} não está disponível. Posso anotar um recado?"
        return f"Sinto muito, {owner} não está disponível agora. Você pode deixar um recado, e eu vou repassar a mensagem."
    if hold_offered:
        return f"Unfortunately, {owner} is not available. Can I take a message?"
    return f"I'm sorry, {owner} is not available right now. You can leave me a message and I'll make sure they get it."


def build_gemini_instruction_text(
    owner_name: str = "",
    hold_offered: bool = False,
    language: Optional[str] = "en",
) -> str:
    """Model instruction for Gemini Live on owner decline/message-taking."""
    owner = owner_name.strip() if isinstance(owner_name, str) and owner_name.strip() else settings.user_name
    lang_note = f" Respond in the caller's language ({language})." if language and language != "en" else ""
    if hold_offered:
        return (
            f"The owner ({owner}) is unavailable. Say unfortunately {owner} is not available and ask if you can take a message.{lang_note} "
            f"Do not offer another availability check or put the caller on hold."
        )
    return (
        f"The owner ({owner}) is unavailable. Tell the caller {owner} is not available and offer to take a message.{lang_note} "
        f"Do not offer to check availability or put the caller on hold."
    )


def build_relay_instruction_text(
    owner_name: str = "",
    hold_offered: bool = False,
    language: Optional[str] = "en",
) -> str:
    """System instruction for ConversationRelay on owner decline/message-taking."""
    owner = owner_name.strip() if isinstance(owner_name, str) and owner_name.strip() else settings.user_name
    from app.services.personal_language import is_portuguese
    if is_portuguese(language):
        if hold_offered:
            return (
                f"SYSTEM INSTRUCTION: {owner} is unavailable. In Brazilian Portuguese (pt-BR), tell the caller unfortunately {owner} is not available "
                f"and ask if you can take a message. Do not offer another availability check."
            )
        return (
            f"SYSTEM INSTRUCTION: {owner} is unavailable. In Brazilian Portuguese (pt-BR), tell the caller and offer to take a message. "
            f"Do not offer to check availability."
        )
    if hold_offered:
        return (
            f"SYSTEM INSTRUCTION: {owner} is unavailable. Tell the caller unfortunately {owner} is not available "
            f"and ask if you can take a message. Do not offer another availability check."
        )
    return (
        f"SYSTEM INSTRUCTION: {owner} is unavailable. Tell the caller and offer to take a message. "
        f"Do not offer to check availability."
    )
