import logging
import re
from enum import Enum
from typing import Tuple, List, TextIO

import click
import requests
import validators
import networkx as nx

from kgx.utils.kgx_utils import get_toolkit, snakecase_to_sentencecase, sentencecase_to_snakecase, \
    camelcase_to_sentencecase
from kgx.prefix_manager import PrefixManager


BIOLINK_MODEL = 'https://biolink.github.io/biolink-model/biolink-model.yaml'
CONTEXT_JSONLD = 'https://biolink.github.io/biolink-model/context.jsonld'


class ErrorType(Enum):
    """
    Validation error types
    """
    MISSING_NODE_PROPERTY = 1
    MISSING_EDGE_PROPERTY = 2
    INVALID_NODE_PROPERTY_VALUE_TYPE = 3
    INVALID_NODE_PROPERTY_VALUE = 4
    INVALID_EDGE_PROPERTY_VALUE_TYPE = 5
    INVALID_EDGE_PROPERTY_VALUE = 6
    NO_CATEGORY = 7
    INVALID_CATEGORY = 8
    NO_EDGE_LABEL = 9
    INVALID_EDGE_LABEL = 10


class MessageLevel(Enum):
    """
    Message level for validation reports
    """
    # Recommendations
    INFO = 1
    # Message to convey 'should'
    WARNING = 2
    # Message to convey 'must'
    ERROR = 3


class ValidationError(object):
    """
    ValidationError class that represents an error.

    Parameters
    ----------
    entity: str
        The node or edge entity that is failing validation
    error_type: kgx.validator.ErrorType
        The nature of the error
    message: str
        The error message
    message_level: kgx.validator.MessageLevel
        The message level

    """
    def __init__(self, entity: str, error_type: ErrorType, message: str, message_level: MessageLevel):
        self.entity = entity
        self.error_type = error_type
        self.message = message
        self.message_level = message_level

    def __str__(self):
        return f"[{self.message_level.name}][{self.error_type.name}] {self.entity} - {self.message}"

    def as_dict(self):
        return {
            'entity': self.entity,
            'error_type': self.error_type,
            'message': self.message,
            'message_level': self.message_level
        }

class Validator(object):
    """
    Class for validating a property graph.


    Parameters
    ----------
    verbose: bool
        Whether the generated report should be verbose or not (default: ``False``)

    """

    def __init__(self, verbose: bool = False):
        self.toolkit = get_toolkit()
        self.prefix_manager = PrefixManager()
        self.prefixes = None
        self.required_node_properties = None
        self.required_edge_properties = None
        self.verbose = verbose

        try:
            self.jsonld = requests.get(CONTEXT_JSONLD).json()
        except:
            raise Exception('Unable to download JSON-LD context from {}'.format(CONTEXT_JSONLD))

    def get_all_prefixes(self) -> set:
        """
        Get all prefixes from Biolink Model JSON-LD context.

        It also sets ``self.prefixes`` for subsequent access.

        Returns
        -------
        set
            A set of prefixes

        """
        if self.prefixes is None:
            prefixes = set(k for k, v in self.jsonld['@context'].items() if isinstance(v, str))
            self.prefixes = prefixes
        return self.prefixes

    def get_required_node_properties(self) -> list:
        """
        Get all properties for a node that are required, as defined by Biolink Model.

        Returns
        -------
        list
            A list of required node properties

        """
        if self.required_node_properties is None:
            node_properties = self.toolkit.children('node property')
            required_properties = []
            for p in node_properties:
                element = self.toolkit.get_element(p)
                if hasattr(element, 'required') and element.required:
                    # TODO: this should be handled by bmt
                    formatted_name = sentencecase_to_snakecase(element.name)
                    required_properties.append(formatted_name)
            self.required_node_properties = required_properties
        return self.required_node_properties

    def get_required_edge_properties(self) -> list:
        """
        Get all properties for an edge that are required, as defined by Biolink Model.

        Returns
        -------
        list
            A list of required edge properties

        """
        if self.required_edge_properties is None:
            edge_properties = self.toolkit.children('association slot')
            required_properties = []
            for p in edge_properties:
                element = self.toolkit.get_element(p)
                if hasattr(element, 'required') and element.required:
                    # TODO: this should be handled by bmt
                    formatted_name = sentencecase_to_snakecase(element.name)
                    required_properties.append(formatted_name)
            self.required_edge_properties = required_properties
        return self.required_edge_properties

    def validate(self, graph: nx.Graph) -> list:
        """
        Validate nodes and edges in a graph.
        TODO: Support strict mode

        Parameters
        ----------
        graph: networkx.Graph
            The graph to validate

        Returns
        -------
        list
            A list of errors for a given graph

        """
        node_errors = self.validate_nodes(graph)
        edge_errors = self.validate_edges(graph)
        return node_errors + edge_errors

    def validate_nodes(self, graph: nx.Graph) -> list:
        """
        Validate all the nodes in a graph.

        This method validates for the following,
        - Node properties
        - Node property type
        - Node property value type
        - Node categories

        Parameters
        ----------
        graph: networkx.Graph
            The graph to validate

        Returns
        -------
        list
            A list of errors for a given graph

        """
        with click.progressbar(graph.nodes(data=True), label='Validating nodes in graph') as bar:
            for n, data in bar:
                e1 = self.validate_node_properties(n, data)
                e2 = self.validate_node_property_types(n, data)
                e3 = self.validate_node_property_values(n, data)
                e4 = self.validate_categories(n, data)
        return e1 + e2 + e3 + e4

    def validate_edges(self, graph: nx.Graph) -> list:
        """
        Validate all the edges in a graph.

        This method validates for the following,
        - Edge properties
        - Edge property type
        - Edge property value type
        - Edge label

        Parameters
        ----------
        graph: networkx.Graph
            The graph to validate

        Returns
        -------
        list
            A list of errors for a given graph

        """
        errors = []
        with click.progressbar(graph.edges(data=True), label='Validate edges in graph') as bar:
            for u, v, data in bar:
                e1 = self.validate_edge_properties(u, v, data)
                e2 = self.validate_edge_property_types(u, v, data)
                e3 = self.validate_edge_property_values(u, v, data)
                e4 = self.validate_edge_label(u, v, data)
                errors += e1 + e2 + e3 + e4
        return errors

    def validate_node_properties(self, node: str, data: dict) -> list:
        """
        Checks if all the required node properties exist for a given node.

        Parameters
        ----------
        node: str
            Node identifier
        data: dict
            Node properties

        Returns
        -------
        list
            A list of errors for a given node

        """
        errors = []
        required_properties = self.get_required_node_properties()
        for p in required_properties:
            if p not in data:
                error_type = ErrorType.MISSING_NODE_PROPERTY
                message = f"Required node property '{p}' missing"
                errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
        return errors

    def validate_edge_properties(self, subject: str, object: str, data: dict) -> list:
        """
        Checks if all the required edge properties exist for a given edge.

        Parameters
        ----------
        subject: str
            Subject identifier
        object: str
            Object identifier
        data: dict
            Edge properties

        Returns
        -------
        list
            A list of errors for a given edge

        """
        errors = []
        required_properties = self.get_required_edge_properties()
        for p in required_properties:
            if p not in data:
                if p == 'association_id':
                    # check for 'id' property instead
                    if 'id' not in data:
                        error_type = ErrorType.MISSING_EDGE_PROPERTY
                        message = f"Required edge property '{p}' missing"
                        errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
                else:
                    error_type = ErrorType.MISSING_EDGE_PROPERTY
                    message = f"Required edge property '{p}' missing"
                    errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
        return errors

    def validate_node_property_types(self, node: str, data: dict) -> list:
        """
        Checks if node properties have the expected value type.

        Parameters
        ----------
        node: str
            Node identifier
        data: dict
            Node properties

        Returns
        -------
        list
            A list of errors for a given node

        """
        errors = []
        error_type = ErrorType.INVALID_NODE_PROPERTY_VALUE_TYPE
        if not isinstance(node, str):
            message = "Node property 'id' expected to be of type 'string'"
            errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))

        for key, value in data.items():
            element = self.toolkit.get_element(key)
            if hasattr(element, 'typeof'):
                if element.typeof == 'string' and not isinstance(value, str):
                    message = f"Node property '{key}' expected to be of type '{element.typeof}'"
                    errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
                elif element.typeof == 'uri' and not isinstance(value, str) and not validators.url(value):
                    message = f"Node property '{key}' expected to be of type {element.typeof}"
                    errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
                elif element.typeof == 'double' and not isinstance(value, (int, float)):
                    message = f"Node property '{key}' expected to be of type '{element.typeof}'"
                    errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
                else:
                    logging.warning("Skipping validation for Node property '{}'. Expected type '{}' vs Actual type '{}'".format(key, element.typeof, type(value)))
            if hasattr(element, 'multivalued'):
                if element.multivalued:
                    if not isinstance(value, list):
                        message = f"Multi-valued node property '{key}' expected to be of type '{list}'"
                        errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
                else:
                    if isinstance(value, (list, set, tuple)):
                        message = f"Single-valued node property '{key}' expected to be of type '{str}'"
                        errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
        return errors

    def validate_edge_property_types(self, subject: str, object: str, data: dict) -> list:
        """
        Checks if edge properties have the expected value type.

        Parameters
        ----------
        subject: str
            Subject identifier
        object: str
            Object identifier
        data: dict
            Edge properties

        Returns
        -------
        list
            A list of errors for a given edge

        """
        errors = []
        error_type = ErrorType.INVALID_EDGE_PROPERTY_VALUE_TYPE
        if not isinstance(subject, str):
            message = "'subject' of an edge expected to be of type 'string'"
            errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
        if not isinstance(object, str):
            message = "'object' of an edge expected to be of type 'string'"
            errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))

        for key, value in data.items():
            element = self.toolkit.get_element(key)
            if hasattr(element, 'typeof'):
                if element.typeof == 'string' and not isinstance(value, str):
                    message = f"Edge property '{key}' expected to be of type 'string'"
                    errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
                elif element.typeof == 'uri' and not isinstance(value, str) and not validators.url(value):
                    message = f"Edge property '{key}' expected to be of type 'uri'"
                    errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
                elif element.typeof == 'double' and not isinstance(value, (int, float)):
                    message = f"Edge property '{key}' expected to be of type 'double'"
                    errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
                else:
                    logging.warning("Skipping validation for Edge property '{}'. Expected type '{}' vs Actual type '{}'".format(key, element.typeof, type(value)))
            if hasattr(element, 'multivalued'):
                if element.multivalued:
                    if not isinstance(value, list):
                        message = f"Multi-valued edge property '{key}' expected to be of type 'list'"
                        errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
                else:
                    if isinstance(value, (list, set, tuple)):
                        message = f"Single-valued edge property '{key}' expected to be of type 'str'"
                        errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
        return errors

    def validate_node_property_values(self, node: str, data: dict) -> list:
        """
        Validate a node property's value.

        Parameters
        ----------
        node: str
            Node identifier
        data: dict
            Node properties

        Returns
        -------
        list
            A list of errors for a given node

        """
        errors = []
        error_type = ErrorType.INVALID_NODE_PROPERTY_VALUE
        if not re.match(r"^[^ :]+:[^ :]+$", node):
            message = f"Node property 'id' expected to be of type 'string'"
            errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
        else:
            prefix = PrefixManager.get_prefix(node)
            if prefix and prefix not in self.get_all_prefixes():
                message = f"Node property 'id' has a value '{node}' with a CURIE prefix '{prefix}' is not represented in Biolink Model JSON-LD context"
                errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
        return errors

    def validate_edge_property_values(self, subject: str, object: str, data: dict) -> list:
        """
        Validate an edge property's value.

        Parameters
        ----------
        subject: str
            Subject identifier
        object: str
            Object identifier
        data: dict
            Edge properties

        Returns
        -------
        list
            A list of errors for a given edge

        """
        errors = []
        error_type = ErrorType.INVALID_EDGE_PROPERTY_VALUE

        if PrefixManager.is_curie(subject):
            prefix = PrefixManager.get_prefix(subject)
            if prefix and prefix not in self.get_all_prefixes():
                message = f"Edge property 'subject' has a value '{subject}' with a CURIE prefix '{prefix}' that is not represented in Biolink Model JSON-LD context"
                errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
        else:
            message = f"Edge property 'subject' has a value '{subject}' which is not a proper CURIE"
            errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))

        if PrefixManager.is_curie(object):
            prefix = PrefixManager.get_prefix(object)
            if prefix not in self.prefixes:
                message = f"Edge property 'object' has a value '{object}' with a CURIE prefix '{prefix}' that is not represented in Biolink Model JSON-LD context"
                errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
        else:
            message = f"Edge property 'object' has a value '{object}' which is not a proper CURIE"
            errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
        return errors

    def validate_categories(self, node: str, data: dict) -> list:
        """
        Validate ``category`` field of a given node.

        Parameters
        ----------
        node: str
            Node identifier
        data: dict
            Node properties

        Returns
        -------
        list
            A list of errors for a given node

        """
        error_type = ErrorType.INVALID_CATEGORY
        errors = []
        categories = data.get('category')
        if categories is None:
            message = "Node does not have a 'category' property"
            errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
        elif not isinstance(categories, list):
            message = f"Node property 'category' expected to be of type {list}"
            errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
        else:
            for category in categories:
                if PrefixManager.is_curie(category):
                    category = PrefixManager.get_reference(category)
                m = re.match(r"^([A-Z][a-z\d]+)+$", category)
                if not m:
                    # category is not CamelCase
                    error_type = ErrorType.INVALID_CATEGORY
                    message = f"Category '{category}' is not in CamelCase form"
                    errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
                formatted_category = camelcase_to_sentencecase(category)
                if not self.toolkit.is_category(formatted_category):
                    message = f"Category '{category}' not in Biolink Model"
                    errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
                else:
                    c = self.toolkit.get_element(formatted_category.lower())
                    if category != c.name and category in c.aliases:
                        message = f"Category {category} is actually an alias for {c.name}; Should replace '{category}' with '{c.name}'"
                        errors.append(ValidationError(node, error_type, message, MessageLevel.ERROR))
        return errors

    def validate_edge_label(self, subject: str, object: str, data: dict) -> list:
        """
        Validate ``edge_label`` field of a given edge.

        Parameters
        ----------
        subject: str
            Subject identifier
        object: str
            Object identifier
        data: dict
            Edge properties

        Returns
        -------
        list
            A list of errors for a given edge

        """
        error_type = ErrorType.INVALID_EDGE_LABEL
        errors = []
        edge_label = data.get('edge_label')
        if edge_label is None:
            message = "Edge does not have an 'edge_label' property"
            errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
        elif not isinstance(edge_label, str):
            message = f"Edge property 'edge_label' expected to be of type 'string'"
            errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
        else:
            if PrefixManager.is_curie(edge_label):
                edge_label = PrefixManager.get_reference(edge_label)
            m = re.match(r"^([a-z_][^A-Z\s]+_?[a-z_][^A-Z\s]+)+$", edge_label)
            if m:
                p = self.toolkit.get_element(snakecase_to_sentencecase(edge_label))
                if p is None:
                    message = f"Edge label '{edge_label}' not in Biolink Model"
                    errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
                elif edge_label != p.name and edge_label in p.aliases:
                    message = f"Edge label '{edge_label}' is actually an alias for {p.name}; Should replace {edge_label} with {p.name}"
                    errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
            else:
                message = f"Edge label '{edge_label}' is not in snake_case form"
                errors.append(ValidationError(f"{subject}-{object}", error_type, message, MessageLevel.ERROR))
        return errors

    def report(self, errors: List[ValidationError]) -> List:
        """
        Prepare error report.

        Parameters
        ----------
        errors: List[ValidationError]
            List of kgx.validator.ValidationError

        Returns
        -------
        List
            A list of formatted errors

        """
        return [str(x) for x in errors]

    def write_report(self, errors: List[ValidationError], outstream: TextIO) -> None:
        """
        Write error report to a file

        Parameters
        ----------
        errors: List[ValidationError]
            List of kgx.validator.ValidationError
        outstream: TextIO
            The stream to write to

        """
        for x in self.report(errors):
            outstream.write(f"{x}\n")
