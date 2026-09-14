"""Read-only loading and inspection of VKG action catalog artifacts."""

from __future__ import annotations

import logging
from pathlib import Path
import re

from rdflib import RDF, Graph, Literal, Namespace, URIRef

from actions.models import ActionDefinition, PropertyClassification
from actions.r2rml_classifier import WriteBackCatalog, classify_writeback

logger = logging.getLogger(__name__)

VKGACT = Namespace("https://databricks.com/ontology/vkg/actions#")
_SUBJECT_TEMPLATE_PLACEHOLDER = re.compile(r"\{[^{}]+\}")


class ActionCatalog:
    """A parsed action catalog, unavailable when its artifact cannot be read."""

    def __init__(
        self,
        *,
        available: bool,
        actions: list[ActionDefinition],
        writeback_catalog: WriteBackCatalog | None = None,
    ) -> None:
        self.available = available
        self.actions = actions
        self.writeback_catalog = writeback_catalog

    @classmethod
    def load(
        cls,
        actions_path: Path | str | None,
        ontology_path: Path | str | None,
        mapping_path: Path | str | None,
    ) -> ActionCatalog:
        """Load actions Turtle without modifying action, ontology, or mapping artifacts."""
        if actions_path is None:
            return cls(available=False, actions=[])

        path = Path(actions_path)
        if not path.is_file():
            logger.info("ActionCatalog: actions file not found at %s", path)
            return cls(available=False, actions=[])

        graph = Graph()
        try:
            graph.parse(path, format="turtle")
        except Exception:
            logger.exception("ActionCatalog: failed to parse %s", path)
            return cls(available=False, actions=[])

        actions = [
            action
            for action in (
                cls._action_definition(graph, subject, "WRITE_BACK")
                for subject in graph.subjects(RDF.type, VKGACT.WriteBackAction)
            )
            if action is not None
        ]
        actions.extend(
            action
            for action in (
                cls._action_definition(graph, subject, "EXTERNAL")
                for subject in graph.subjects(RDF.type, VKGACT.ExternalAction)
            )
            if action is not None
        )
        actions.sort(key=lambda action: action.logical_key)
        writeback_catalog = cls._load_writeback_catalog(
            mapping_path, ontology_path, actions
        )
        logger.info("ActionCatalog: loaded %s (%d actions)", path, len(actions))
        return cls(
            available=True,
            actions=actions,
            writeback_catalog=writeback_catalog,
        )

    @staticmethod
    def _load_writeback_catalog(
        mapping_path: Path | str | None,
        ontology_path: Path | str | None,
        actions: list[ActionDefinition],
    ) -> WriteBackCatalog | None:
        if mapping_path is None or not Path(mapping_path).is_file():
            return None
        try:
            mapping_graph = Graph().parse(Path(mapping_path), format="turtle")
            ontology_graph = None
            if ontology_path is not None and Path(ontology_path).is_file():
                ontology_graph = Graph().parse(Path(ontology_path), format="turtle")
            return classify_writeback(mapping_graph, ontology_graph, actions)
        except Exception:
            logger.exception("ActionCatalog: failed to classify write-back mappings")
            return None

    @staticmethod
    def _action_definition(
        graph: Graph, subject: URIRef, kind: str
    ) -> ActionDefinition | None:
        logical_key = _literal_value(graph.value(subject, VKGACT.logicalKey))
        if not logical_key:
            logger.warning("ActionCatalog: ignoring %s without act:logicalKey", subject)
            return None

        return ActionDefinition(
            iri=str(subject),
            logical_key=logical_key,
            kind=kind,  # type: ignore[arg-type]
            bound_class_iri=_iri_value(graph.value(subject, VKGACT.boundClass)),
            target_property_iri=_iri_value(graph.value(subject, VKGACT.targetProperty)),
            function_fqn=(
                _literal_value(graph.value(subject, VKGACT.functionFqn))
                or _literal_value(graph.value(subject, VKGACT.invokesFunction))
            ),
            status=_literal_value(graph.value(subject, VKGACT.status)) or "DRAFT",
            input_schema=_parameter_schema(graph, subject, VKGACT.inputParameter),
            output_schema=_parameter_schema(graph, subject, VKGACT.outputParameter),
            approval_policy=_term_name(graph.value(subject, VKGACT.approvalPolicy))
            or "HumanConfirm",
            timeout_seconds=_positive_int(
                graph.value(subject, VKGACT.timeoutSeconds), 60
            ),
            max_attempts=_positive_int(graph.value(subject, VKGACT.maxAttempts), 1),
            idempotency_strategy=(
                _literal_value(graph.value(subject, VKGACT.idempotencyStrategy))
                or "REQUEST_KEY"
            ),
        )

    def list_actions(
        self,
        *,
        class_iri: str | None = None,
        subject_iri: str | None = None,
        property_iri: str | None = None,
        kind: str | None = None,
    ) -> dict[str, object]:
        """List actions filtered by catalog metadata and proven subject templates.

        External actions have no action-specific subject template, so they do
        not match a ``subject_iri`` filter.
        """
        classifications = (
            self.writeback_catalog.classifications
            if self.writeback_catalog is not None
            else ()
        )
        actions = [
            action
            for action in self.actions
            if (class_iri is None or action.bound_class_iri == class_iri)
            and (property_iri is None or action.target_property_iri == property_iri)
            and (kind is None or action.kind == kind)
            and (
                subject_iri is None
                or (
                    action.kind == "WRITE_BACK"
                    and self.writeback_catalog is not None
                    and (target := self.writeback_catalog.target_for_action(action.iri))
                    is not None
                    and _subject_matches_template(subject_iri, target.subject_template)
                )
            )
        ]
        return {
            "available": self.available,
            "actions": [self._as_dict(action) for action in actions],
            "properties": [
                self._property_as_dict(classification)
                for classification in classifications
                if (class_iri is None or classification.class_iri == class_iri)
                and (
                    property_iri is None or classification.property_iri == property_iri
                )
                and (kind is None or kind == "WRITE_BACK")
                and (
                    subject_iri is None
                    or (
                        classification.target is not None
                        and _subject_matches_template(
                            subject_iri, classification.target.subject_template
                        )
                    )
                )
            ],
        }

    def describe_action(self, action_iri: str) -> dict[str, object] | None:
        for action in self.actions:
            if action.iri == action_iri:
                return self._as_dict(action)
        return None

    @staticmethod
    def _as_dict(action: ActionDefinition) -> dict[str, object]:
        return {
            "iri": action.iri,
            "logical_key": action.logical_key,
            "kind": action.kind,
            "bound_class_iri": action.bound_class_iri,
            "target_property_iri": action.target_property_iri,
            "function_fqn": action.function_fqn,
            "status": action.status,
            "input_schema": action.input_schema,
            "output_schema": action.output_schema,
            "approval_policy": action.approval_policy,
            "timeout_seconds": action.timeout_seconds,
            "max_attempts": action.max_attempts,
            "idempotency_strategy": action.idempotency_strategy,
        }

    @staticmethod
    def _property_as_dict(
        classification: PropertyClassification,
    ) -> dict[str, object]:
        return {
            "class_iri": classification.class_iri,
            "property_iri": classification.property_iri,
            "writable": classification.writable,
            "reasons": list(classification.reasons),
            "action_iri": classification.action_iri,
        }


def _literal_value(value: object) -> str | None:
    return str(value) if isinstance(value, Literal) else None


def _iri_value(value: object) -> str | None:
    return str(value) if isinstance(value, URIRef) else None


def _term_name(value: object) -> str | None:
    if isinstance(value, URIRef):
        return str(value).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    return _literal_value(value)


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _subject_matches_template(subject_iri: str, subject_template: str) -> bool:
    """Return whether an IRI is provably represented by an R2RML template."""
    pattern = ".+".join(
        re.escape(part)
        for part in _SUBJECT_TEMPLATE_PLACEHOLDER.split(subject_template)
    )
    return re.fullmatch(pattern, subject_iri) is not None


def _parameter_schema(
    graph: Graph, action: URIRef, predicate: URIRef
) -> dict[str, str]:
    schema: dict[str, str] = {}
    for parameter in graph.objects(action, predicate):
        name = _literal_value(graph.value(parameter, VKGACT.parameterName))
        datatype = _iri_value(graph.value(parameter, VKGACT.datatype))
        if name and datatype:
            schema[name] = datatype
    return schema
