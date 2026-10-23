"""Conservative inversion of R2RML mappings for write-back actions."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from rdflib import Graph, Namespace, RDF, URIRef
from sqlglot import exp, parse_one

from actions.models import ActionDefinition, PropertyClassification, WriteBackTarget

RR = Namespace("http://www.w3.org/ns/r2rml#")
OWL = Namespace("http://www.w3.org/2002/07/owl#")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class _SqlShape:
    table_fqn: tuple[str, str, str]
    alias_to_column: dict[str, str]
    non_column_aliases: frozenset[str]
    has_computed_projection: bool
    where_not_null_column: str | None


@dataclass(frozen=True)
class _PropertyMap:
    class_iri: str
    property_iri: str
    table_fqn: tuple[str, str, str] | None
    alias_to_column: dict[str, str]
    non_column_aliases: frozenset[str]
    has_computed_projection: bool
    where_not_null_column: str | None
    subject_template: str | None
    key_alias: str | None
    value_alias: str | None
    source_aliases: tuple[str, ...]
    datatype_iri: str | None
    reasons: tuple[str, ...]


class WriteBackCatalog:
    def __init__(self, classifications: Sequence[PropertyClassification]) -> None:
        self.classifications = tuple(classifications)
        self._by_property = {
            (item.class_iri, item.property_iri): item for item in self.classifications
        }
        self._targets = {
            item.action_iri: item.target
            for item in self.classifications
            if item.action_iri is not None and item.target is not None
        }

    def target_for_action(self, action_iri: str) -> WriteBackTarget | None:
        return self._targets.get(action_iri)

    def property_for(
        self, class_iri: str, property_iri: str
    ) -> PropertyClassification | None:
        return self._by_property.get((class_iri, property_iri))


def classify_writeback(
    mapping_graph: Graph,
    ontology_graph: Graph | None,
    actions: Sequence[ActionDefinition],
) -> WriteBackCatalog:
    """Classify every mapped property and declared write-back action.

    Ambiguity is intentionally a refusal. This output is used as a runtime safety
    boundary, so it never guesses an inverse path for a mapping shape it cannot prove.
    """
    mapped_properties = _read_property_maps(mapping_graph)
    coupled_pairs = _coupled_pairs(mapped_properties)
    by_pair: dict[tuple[str, str], list[_PropertyMap]] = {}
    for property_map in mapped_properties:
        by_pair.setdefault(
            (property_map.class_iri, property_map.property_iri), []
        ).append(property_map)

    write_actions = [action for action in actions if action.kind == "WRITE_BACK"]
    actions_by_pair: dict[tuple[str, str], list[ActionDefinition]] = {}
    for action in write_actions:
        if action.bound_class_iri and action.target_property_iri:
            actions_by_pair.setdefault(
                (action.bound_class_iri, action.target_property_iri), []
            ).append(action)

    classifications: list[PropertyClassification] = []
    for pair in sorted(set(by_pair) | set(actions_by_pair)):
        candidates = by_pair.get(pair, [])
        pair_actions = actions_by_pair.get(pair, [])
        action = pair_actions[0] if len(pair_actions) == 1 else None
        reasons = _reasons_for_pair(
            candidates, action, ontology_graph, pair in coupled_pairs
        )
        target = _target_for_pair(candidates, action, reasons)
        classifications.append(
            PropertyClassification(
                class_iri=pair[0],
                property_iri=pair[1],
                writable=target is not None,
                reasons=tuple(reasons),
                action_iri=action.iri if action else None,
                target=target,
            )
        )
    return WriteBackCatalog(classifications)


def _read_property_maps(graph: Graph) -> list[_PropertyMap]:
    property_maps: list[_PropertyMap] = []
    for triples_map in graph.subjects(RDF.type, RR.TriplesMap):
        logical_tables = list(graph.objects(triples_map, RR.logicalTable))
        logical_table = logical_tables[0] if logical_tables else None
        (
            table_fqn,
            aliases,
            non_column_aliases,
            has_computed_projection,
            where_not_null_column,
            logical_reasons,
        ) = _logical_table(graph, logical_table)
        if len(logical_tables) != 1:
            logical_reasons.append("UNSUPPORTED_LOGICAL_TABLE")
        subject_maps = list(graph.objects(triples_map, RR.subjectMap))
        subject_map = subject_maps[0] if subject_maps else None
        classes = sorted(
            {
                str(value)
                for item in subject_maps
                for value in graph.objects(item, RR["class"])
                if isinstance(value, URIRef)
            }
        )
        templates = [
            _string(value) for value in graph.objects(subject_map, RR.template)
        ]
        template = templates[0] if len(templates) == 1 else None
        placeholders = _PLACEHOLDER.findall(template or "")
        subject_reasons: list[str] = []
        key_alias: str | None = None
        if (
            len(subject_maps) != 1
            or len(classes) != 1
            or template is None
            or len(templates) != 1
            or len(placeholders) != 1
        ):
            subject_reasons.append("UNSUPPORTED_SUBJECT_TEMPLATE")
        else:
            key_alias = placeholders[0]

        for pom in graph.objects(triples_map, RR.predicateObjectMap):
            predicates = [
                str(value)
                for value in graph.objects(pom, RR.predicate)
                if isinstance(value, URIRef)
            ]
            object_maps = list(graph.objects(pom, RR.objectMap))
            object_map = object_maps[0] if len(object_maps) == 1 else None
            columns = list(graph.objects(object_map, RR.column))
            datatypes = list(graph.objects(object_map, RR.datatype))
            term_types = list(graph.objects(object_map, RR.termType))
            value_alias = _string(columns[0]) if len(columns) == 1 else None
            datatype = _iri(datatypes[0]) if len(datatypes) == 1 else None
            source_aliases = _source_aliases(graph, object_maps)
            object_reasons: list[str] = []
            if len(predicates) != 1 or list(graph.objects(pom, RR.predicateMap)):
                object_reasons.append("MULTIPLE_PROPERTY_MAPPINGS")
            if len(object_maps) != 1:
                object_reasons.append("MULTIPLE_PROPERTY_MAPPINGS")
            elif (
                object_map is None
                or len(columns) != 1
                or len(datatypes) > 1
                or len(term_types) > 1
                or value_alias is None
                or list(graph.objects(pom, RR.object))
                or _has_unsupported_object_shape(graph, object_map)
            ):
                object_reasons.append("UNSUPPORTED_OBJECT_MAP")
            elif term_types and term_types[0] in (RR.IRI, RR.BlankNode):
                object_reasons.append("NOT_DATATYPE_PROPERTY")
            for class_iri in classes:
                for property_iri in predicates:
                    property_maps.append(
                        _PropertyMap(
                            class_iri=class_iri,
                            property_iri=property_iri,
                            table_fqn=table_fqn,
                            alias_to_column=aliases,
                            non_column_aliases=non_column_aliases,
                            has_computed_projection=has_computed_projection,
                            where_not_null_column=where_not_null_column,
                            subject_template=template,
                            key_alias=key_alias,
                            value_alias=value_alias,
                            source_aliases=source_aliases,
                            datatype_iri=datatype,
                            reasons=tuple(
                                logical_reasons + subject_reasons + object_reasons
                            ),
                        )
                    )
    return property_maps


def _logical_table(
    graph: Graph, logical_table: object
) -> tuple[
    tuple[str, str, str] | None,
    dict[str, str],
    frozenset[str],
    bool,
    str | None,
    list[str],
]:
    if logical_table is None:
        return None, {}, frozenset(), False, None, ["UNSUPPORTED_LOGICAL_TABLE"]
    sql_queries = list(graph.objects(logical_table, RR.sqlQuery))
    table_names = list(graph.objects(logical_table, RR.tableName))
    if len(sql_queries) == 1 and not table_names:
        sql = _string(sql_queries[0])
        try:
            shape = _parse_sql_shape(sql)
        except _SqlShapeError as error:
            return None, {}, frozenset(), False, None, [error.reason]
        return (
            shape.table_fqn,
            shape.alias_to_column,
            shape.non_column_aliases,
            shape.has_computed_projection,
            shape.where_not_null_column,
            [],
        )

    if len(table_names) != 1 or sql_queries:
        return None, {}, frozenset(), False, None, ["UNSUPPORTED_LOGICAL_TABLE"]
    table_name = _string(table_names[0])
    table_fqn = _table_fqn(table_name) if table_name else None
    if table_fqn is None:
        return None, {}, frozenset(), False, None, ["UNSUPPORTED_LOGICAL_TABLE"]
    return table_fqn, {}, frozenset(), False, None, []


class _SqlShapeError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _parse_sql_shape(sql: str) -> _SqlShape:
    try:
        query = parse_one(sql, read="databricks")
    except Exception as error:
        raise _SqlShapeError("UNSUPPORTED_LOGICAL_TABLE") from error
    if not isinstance(query, exp.Select):
        raise _SqlShapeError("JOINED_OR_COMPUTED_QUERY")
    if (
        query.args.get("distinct")
        or query.args.get("group")
        or query.args.get("having")
        or query.find(exp.AggFunc)
        or query.find(exp.Window)
    ):
        raise _SqlShapeError("DISTINCT_OR_AGGREGATED_QUERY")
    if query.args.get("joins") or query.find(exp.Join) or query.find(exp.Subquery):
        raise _SqlShapeError("JOINED_OR_COMPUTED_QUERY")
    if any(
        query.args.get(clause)
        for clause in (
            "limit",
            "offset",
            "order",
            "qualify",
            "cluster",
            "distribute",
            "sort",
            "with_",
        )
    ):
        raise _SqlShapeError("JOINED_OR_COMPUTED_QUERY")
    from_clause = query.args.get("from")
    table = from_clause.this if isinstance(from_clause, exp.From) else None
    if not isinstance(table, exp.Table) or from_clause.expressions:
        raise _SqlShapeError("JOINED_OR_COMPUTED_QUERY")
    if set(table.args) - {"this", "db", "catalog", "alias"}:
        raise _SqlShapeError("JOINED_OR_COMPUTED_QUERY")
    table_fqn = _table_fqn_from_expression(table)
    if table_fqn is None:
        raise _SqlShapeError("UNSUPPORTED_LOGICAL_TABLE")
    where_column = None
    if query.args.get("where"):
        where_column = _not_null_column(query.args["where"].this)
    if query.args.get("where") and where_column is None:
        raise _SqlShapeError("JOINED_OR_COMPUTED_QUERY")

    alias_to_column: dict[str, str] = {}
    non_column_aliases: set[str] = set()
    seen_aliases: set[str] = set()
    for projection in query.expressions:
        if not isinstance(projection, exp.Alias) or not projection.alias:
            raise _SqlShapeError("JOINED_OR_COMPUTED_QUERY")
        normalized_alias = projection.alias.casefold()
        if normalized_alias in seen_aliases:
            raise _SqlShapeError("JOINED_OR_COMPUTED_QUERY")
        seen_aliases.add(normalized_alias)
        if isinstance(projection.this, exp.Column) and not projection.this.table:
            alias_to_column[normalized_alias] = projection.this.name
        else:
            non_column_aliases.add(normalized_alias)
    return _SqlShape(
        table_fqn,
        alias_to_column,
        frozenset(non_column_aliases),
        bool(non_column_aliases),
        where_column,
    )


def _not_null_column(expression: exp.Expression) -> str | None:
    if (
        isinstance(expression, exp.Not)
        and isinstance(expression.this, exp.Is)
        and isinstance(expression.this.this, exp.Column)
        and isinstance(expression.this.expression, exp.Null)
    ):
        return expression.this.this.name
    return None


def _reasons_for_pair(
    candidates: Sequence[_PropertyMap],
    action: ActionDefinition | None,
    ontology: Graph | None,
    coupled: bool,
) -> list[str]:
    if action is None:
        return ["NO_ACTION_DECLARED"]
    if not candidates:
        return ["NO_R2RML_MAPPING"]
    if len(candidates) != 1:
        return ["MULTIPLE_PROPERTY_MAPPINGS"]
    candidate = candidates[0]
    reasons = list(candidate.reasons)
    if (
        ontology is not None
        and (URIRef(candidate.property_iri), RDF.type, OWL.DatatypeProperty)
        not in ontology
    ):
        reasons.append("NOT_DATATYPE_PROPERTY")
    if candidate.key_alias is not None:
        key_alias = _identifier_key(candidate.key_alias)
        if key_alias in candidate.non_column_aliases:
            reasons.append("KEY_MAPPING_NOT_PLAIN_COLUMN")
        elif key_alias not in candidate.alias_to_column and candidate.alias_to_column:
            reasons.append("KEY_MAPPING_NOT_PLAIN_COLUMN")
    if candidate.has_computed_projection:
        reasons.append("JOINED_OR_COMPUTED_QUERY")
    if candidate.where_not_null_column is not None and candidate.key_alias is not None:
        key_column = candidate.alias_to_column.get(
            _identifier_key(candidate.key_alias), candidate.key_alias
        )
        if _identifier_key(key_column) != _identifier_key(
            candidate.where_not_null_column
        ):
            reasons.append("JOINED_OR_COMPUTED_QUERY")
    if candidate.value_alias is not None:
        value_alias = _identifier_key(candidate.value_alias)
        if value_alias in candidate.non_column_aliases:
            reasons.append("VALUE_MAPPING_NOT_PLAIN_COLUMN")
        elif value_alias not in candidate.alias_to_column and candidate.alias_to_column:
            reasons.append("VALUE_MAPPING_NOT_PLAIN_COLUMN")
    if coupled:
        reasons.append("COUPLED_SOURCE_COLUMN")
    if _same_identity_and_value(candidate):
        reasons.append("IDENTITY_COLUMN_UPDATE")
    return list(dict.fromkeys(reasons))


def _coupled_pairs(properties: Sequence[_PropertyMap]) -> set[tuple[str, str]]:
    by_source: dict[tuple[str, str, str, str], set[tuple[str, str]]] = {}
    for property_map in properties:
        if property_map.table_fqn is None:
            continue
        for source_alias in property_map.source_aliases:
            value_column = property_map.alias_to_column.get(
                _identifier_key(source_alias), source_alias
            )
            source = tuple(
                _identifier_key(identifier)
                for identifier in (*property_map.table_fqn, value_column)
            )
            by_source.setdefault(source, set()).add(
                (property_map.class_iri, property_map.property_iri)
            )
    return {pair for pairs in by_source.values() if len(pairs) > 1 for pair in pairs}


def _target_for_pair(
    candidates: Sequence[_PropertyMap],
    action: ActionDefinition | None,
    reasons: Sequence[str],
) -> WriteBackTarget | None:
    if action is None or reasons or len(candidates) != 1:
        return None
    candidate = candidates[0]
    if (
        candidate.table_fqn is None
        or candidate.key_alias is None
        or candidate.value_alias is None
        or candidate.subject_template is None
    ):
        return None
    key_column = candidate.alias_to_column.get(
        _identifier_key(candidate.key_alias), candidate.key_alias
    )
    value_column = candidate.alias_to_column.get(
        _identifier_key(candidate.value_alias), candidate.value_alias
    )
    return WriteBackTarget(
        action_iri=action.iri,
        class_iri=candidate.class_iri,
        property_iri=candidate.property_iri,
        table_fqn=candidate.table_fqn,
        key_column=key_column,
        value_column=value_column,
        key_alias=candidate.key_alias,
        value_alias=candidate.value_alias,
        subject_template=candidate.subject_template,
        datatype_iri=candidate.datatype_iri,
    )


def _same_identity_and_value(candidate: _PropertyMap) -> bool:
    if candidate.key_alias is None or candidate.value_alias is None:
        return False
    key = candidate.alias_to_column.get(
        _identifier_key(candidate.key_alias), candidate.key_alias
    )
    value = candidate.alias_to_column.get(
        _identifier_key(candidate.value_alias), candidate.value_alias
    )
    return _identifier_key(key) == _identifier_key(value)


def _has_unsupported_object_shape(graph: Graph, object_map: object) -> bool:
    return any(
        any(graph.objects(object_map, predicate))
        for predicate in (
            RR.template,
            RR.constant,
            RR.language,
            RR.parentTriplesMap,
            RR.inverseExpression,
        )
    )


def _source_aliases(graph: Graph, object_maps: Sequence[object]) -> tuple[str, ...]:
    aliases: set[str] = set()
    for object_map in object_maps:
        for value in graph.objects(object_map, RR.column):
            column = _string(value)
            if column is not None:
                aliases.add(column)
        for value in graph.objects(object_map, RR.template):
            template = _string(value)
            if template is not None:
                aliases.update(_PLACEHOLDER.findall(template))
    return tuple(sorted(aliases))


def _table_fqn_from_expression(table: exp.Table) -> tuple[str, str, str] | None:
    if table.alias or not table.catalog or not table.db or not table.name:
        return None
    return table.catalog, table.db, table.name


def _table_fqn(table_name: str | None) -> tuple[str, str, str] | None:
    if not table_name:
        return None
    parts = [part.strip().strip("`") for part in table_name.split(".")]
    return tuple(parts) if len(parts) == 3 and all(parts) else None  # type: ignore[return-value]


def _identifier_key(value: str) -> str:
    """Return the comparison key for an unquoted Databricks identifier."""
    return value.casefold()


def _string(value: object) -> str | None:
    return str(value) if value is not None else None


def _iri(value: object) -> str | None:
    return str(value) if isinstance(value, URIRef) else None
