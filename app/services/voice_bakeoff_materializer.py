"""Closed, non-authorizing content proposals for the offline voice bakeoff."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from app.services.dialogue_planner import ActionName, NextAction
from app.services.voice_call_lifecycle import CallIntentKind
from app.services.voice_lifecycle import VoiceSemanticActKind
from app.services.voice_speech_control import SemanticAct, SpokenPlan


class ProposalKind(str, Enum):
    PLANNED = "planned"
    INPUT_REPAIR = "input_repair"
    PRESENCE_CHECK = "presence_check"
    MORE_TIME_ACKNOWLEDGEMENT = "more_time_acknowledgement"
    SILENCE_CLOSURE = "silence_closure"


@dataclass(frozen=True, slots=True)
class ContentProposal:
    """A typed content proposal. It carries no speech authority."""

    proposal_kind: ProposalKind
    action_name: ActionName | None
    state_version: int
    locale: str
    plan: SpokenPlan
    proposal_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.proposal_kind, ProposalKind)
            or (
                self.proposal_kind is ProposalKind.PLANNED
                and not isinstance(self.action_name, ActionName)
            )
            or (
                self.proposal_kind is not ProposalKind.PLANNED
                and self.action_name is not None
            )
            or type(self.state_version) is not int
            or self.state_version < 0
            or self.locale not in _CATALOG
            or not isinstance(self.plan, SpokenPlan)
            or not _digest(self.proposal_digest)
        ):
            raise ValueError("content proposal is invalid")
        if self.proposal_digest != _proposal_digest(
            proposal_kind=self.proposal_kind,
            action_name=self.action_name,
            state_version=self.state_version,
            locale=self.locale,
            plan=self.plan,
        ):
            raise ValueError("content proposal digest is invalid")


class FixedProposalMaterializer:
    """Select reviewed fixed assets; never creates authorization or interpolates facts."""

    def materialize(
        self,
        *,
        action: NextAction,
        state_version: int,
        locale: str,
        safe_facts: tuple[str, ...] = (),
    ) -> ContentProposal:
        if (
            not isinstance(action, NextAction)
            or type(state_version) is not int
            or state_version < 0
            or not isinstance(safe_facts, tuple)
            or safe_facts
        ):
            raise ValueError("materializer input is invalid")
        selected_locale = _locale(locale)
        acts = _planned_acts(action, selected_locale)
        return _proposal(
            proposal_kind=ProposalKind.PLANNED,
            action_name=action.name,
            state_version=state_version,
            locale=selected_locale,
            acts=acts,
        )

    def input_repair(
        self,
        *,
        state_version: int,
        locale: str,
    ) -> ContentProposal:
        if type(state_version) is not int or state_version < 0:
            raise ValueError("materializer state version is invalid")
        selected_locale = _locale(locale)
        return _proposal(
            proposal_kind=ProposalKind.INPUT_REPAIR,
            action_name=None,
            state_version=state_version,
            locale=selected_locale,
            acts=(
                SemanticAct(
                    VoiceSemanticActKind.REPAIR,
                    _CATALOG[selected_locale]["input_repair"],
                ),
            ),
        )

    def lifecycle_act(
        self,
        *,
        intent_kind: CallIntentKind,
        state_version: int,
        locale: str,
    ) -> ContentProposal:
        if (
            not isinstance(intent_kind, CallIntentKind)
            or type(state_version) is not int
            or state_version < 0
        ):
            raise ValueError("lifecycle materializer input is invalid")
        selected_locale = _locale(locale)
        mapping = {
            CallIntentKind.REQUEST_PRESENCE_CHECK: (
                ProposalKind.PRESENCE_CHECK,
                VoiceSemanticActKind.PRESENCE_CHECK,
                "presence_check",
            ),
            CallIntentKind.REQUEST_MORE_TIME_ACKNOWLEDGEMENT: (
                ProposalKind.MORE_TIME_ACKNOWLEDGEMENT,
                VoiceSemanticActKind.ACKNOWLEDGEMENT,
                "more_time_acknowledgement",
            ),
            CallIntentKind.REQUEST_CLOSING: (
                ProposalKind.SILENCE_CLOSURE,
                VoiceSemanticActKind.CLOSING,
                "silence_closure",
            ),
        }
        selected = mapping.get(intent_kind)
        if selected is None:
            raise ValueError("lifecycle intent has no reviewed asset")
        proposal_kind, semantic_kind, catalog_key = selected
        try:
            text = _CATALOG[selected_locale][catalog_key]
        except KeyError as error:
            raise ValueError(
                "no reviewed lifecycle asset exists"
            ) from error
        return _proposal(
            proposal_kind=proposal_kind,
            action_name=None,
            state_version=state_version,
            locale=selected_locale,
            acts=(
                SemanticAct(
                    semantic_kind,
                    text,
                ),
            ),
        )


_CATALOG_DATA: dict[str, dict[str, str]] = {
    "en": {
        "answer": "Pricing depends on the job details.",
        "service_action": "Is this a repair, replacement, installation, or inspection?",
        "service_object": "Which fixture, appliance, or system needs help?",
        "job_complexity": "What details would help us understand the job?",
        "urgency": "How urgent is the work?",
        "caller_name": "May I have your name?",
        "callback_preference": "Would you like a callback or scheduling help?",
        "callback_confirmation": "Is the callback number ending shown correct?",
        "callback_number": "What is the best callback number?",
        "service_address": "What is the service address?",
        "safety": "If there is immediate danger, call emergency services now.",
        "safety_location": "Are you in a safe location?",
        "acknowledgement": "Thank you. I have the details.",
        "decline": "I can only help with this business's services.",
        "input_repair": "Sorry, I did not understand. Please say that again.",
        "presence_check": "Are you still there?",
        "more_time_acknowledgement":
            "Take your time. I’ll wait twenty more seconds.",
        "silence_closure":
            "I can’t hear a response, so I’ll end this test call now. Goodbye.",
    },
    "es": {
        "answer": "El precio depende de los detalles del trabajo.",
        "service_action": "Es reparación, reemplazo, instalación o inspección?",
        "service_object": "Qué equipo o sistema necesita ayuda?",
        "job_complexity": "Qué detalles ayudan a entender el trabajo?",
        "urgency": "Qué tan urgente es el trabajo?",
        "caller_name": "Me puede decir su nombre?",
        "callback_preference": "Prefiere una llamada o ayuda para programar?",
        "callback_confirmation": "Es correcto el número de devolución mostrado?",
        "callback_number": "Cuál es el mejor número para devolver la llamada?",
        "service_address": "Cuál es la dirección del servicio?",
        "safety": "Si hay peligro inmediato, llame a los servicios de emergencia.",
        "safety_location": "Está en un lugar seguro?",
        "acknowledgement": "Gracias. Ya tengo los detalles.",
        "decline": "Solo puedo ayudar con los servicios de este negocio.",
        "input_repair": "Perdón, no entendí. Puede repetirlo?",
        "presence_check": "¿Sigue ahí?",
        "more_time_acknowledgement":
            "Tómese su tiempo. Esperaré veinte segundos más.",
        "silence_closure":
            "No escucho una respuesta, así que finalizaré esta llamada de prueba ahora. Adiós.",
    },
    "pt": {
        "answer": "O preço depende dos detalhes do serviço.",
        "service_action": "É reparo, troca, instalação ou inspeção?",
        "service_object": "Qual equipamento ou sistema precisa de ajuda?",
        "job_complexity": "Quais detalhes ajudam a entender o serviço?",
        "urgency": "Qual é a urgência do serviço?",
        "caller_name": "Pode me dizer seu nome?",
        "callback_preference": "Prefere retorno ou ajuda para agendar?",
        "callback_confirmation": "O número de retorno mostrado está correto?",
        "callback_number": "Qual é o melhor número para retorno?",
        "service_address": "Qual é o endereço do serviço?",
        "safety": "Se houver perigo imediato, ligue para os serviços de emergência.",
        "safety_location": "Você está em um local seguro?",
        "acknowledgement": "Obrigado. Já tenho os detalhes.",
        "decline": "Só posso ajudar com os serviços desta empresa.",
        "input_repair": "Desculpe, não entendi. Pode repetir?",
        "presence_check": "Você ainda está aí?",
        "more_time_acknowledgement": "Sem pressa. Vou esperar mais vinte segundos.",
        "silence_closure": "Não consigo ouvir uma resposta, então vou encerrar esta chamada de teste agora. Até logo.",
    },
    "zh": {
        "answer": "价格取决于具体的服务情况。",
        "service_action": "这是维修、更换、安装还是检查？",
        "service_object": "哪个设备或系统需要帮助？",
        "job_complexity": "还有哪些细节可以帮助我们了解这项服务？",
        "urgency": "这项服务有多紧急？",
        "caller_name": "请问您叫什么名字？",
        "callback_preference": "您希望我们回电还是帮助安排时间？",
        "callback_confirmation": "显示的回电号码正确吗？",
        "callback_number": "最合适的回电号码是什么？",
        "service_address": "服务地址是什么？",
        "safety": "如果有直接危险，请立即联系紧急服务。",
        "safety_location": "您现在在安全的地方吗？",
        "acknowledgement": "谢谢，我已经记下这些信息。",
        "decline": "我只能协助处理这家公司的服务。",
        "input_repair": "抱歉，我没有听懂。请再说一遍。",
        "presence_check": "请问您还在吗？",
        "more_time_acknowledgement":
            "您慢慢来。我会再等二十秒。",
        "silence_closure":
            "我没有听到回应，所以现在结束这次测试通话。再见。",
    },
}
_CATALOG = MappingProxyType(
    {
        locale: MappingProxyType(entries)
        for locale, entries in _CATALOG_DATA.items()
    }
)
del _CATALOG_DATA


def _planned_acts(
    action: NextAction,
    locale: str,
) -> tuple[SemanticAct, ...]:
    catalog = _CATALOG[locale]
    acts: list[SemanticAct] = []
    if action.name is ActionName.ANSWER_DIRECT_QUESTION:
        acts.append(SemanticAct(VoiceSemanticActKind.ANSWER, catalog["answer"]))
    elif action.name is ActionName.SAFETY_GUIDANCE:
        acts.append(SemanticAct(VoiceSemanticActKind.SAFETY, catalog["safety"]))
    elif action.name is ActionName.DECLINE_OUT_OF_SCOPE:
        acts.append(
            SemanticAct(
                VoiceSemanticActKind.ACKNOWLEDGEMENT,
                catalog["decline"],
            )
        )
    elif not action.question_required:
        acts.append(
            SemanticAct(
                VoiceSemanticActKind.ACKNOWLEDGEMENT,
                catalog["acknowledgement"],
            )
        )
    if action.question_required:
        slot = action.allowed_slots[0]
        if slot not in catalog:
            raise ValueError("no reviewed question asset exists")
        acts.append(
            SemanticAct(
                VoiceSemanticActKind.QUESTION,
                catalog[slot],
                question_slot=slot,
            )
        )
    return tuple(acts)


def _proposal(
    *,
    proposal_kind: ProposalKind,
    action_name: ActionName | None,
    state_version: int,
    locale: str,
    acts: tuple[SemanticAct, ...],
) -> ContentProposal:
    provisional = SpokenPlan(plan_id="pending", acts=acts)
    digest = _proposal_digest(
        proposal_kind=proposal_kind,
        action_name=action_name,
        state_version=state_version,
        locale=locale,
        plan=provisional,
        include_plan_id=False,
    )
    plan = SpokenPlan(plan_id=f"plan_{digest}", acts=acts)
    return ContentProposal(
        proposal_kind=proposal_kind,
        action_name=action_name,
        state_version=state_version,
        locale=locale,
        plan=plan,
        proposal_digest=_proposal_digest(
            proposal_kind=proposal_kind,
            action_name=action_name,
            state_version=state_version,
            locale=locale,
            plan=plan,
        ),
    )


def _proposal_digest(
    *,
    proposal_kind: ProposalKind,
    action_name: ActionName | None,
    state_version: int,
    locale: str,
    plan: SpokenPlan,
    include_plan_id: bool = True,
) -> str:
    material = {
        "domain": "hey-kevin/offline-content-proposal/v1",
        "proposal_kind": proposal_kind.value,
        "action_name": None if action_name is None else action_name.value,
        "state_version": state_version,
        "locale": locale,
        "plan_id": plan.plan_id if include_plan_id else None,
        "acts": [
            {
                "kind": act.kind.value,
                "text": act.text,
                "question_slot": act.question_slot,
                "private_disclosure": act.private_disclosure,
                "unsupported_promise": act.unsupported_promise,
                "complete": act.complete,
            }
            for act in plan.acts
        ],
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _locale(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("locale is invalid")
    normalized = value.strip().casefold()
    if normalized in {"", "unknown", "und"}:
        return "en"
    prefix = normalized.split("-", 1)[0]
    if prefix not in _CATALOG:
        raise ValueError("no reviewed locale assets exist")
    return prefix


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "ContentProposal",
    "FixedProposalMaterializer",
    "ProposalKind",
]
