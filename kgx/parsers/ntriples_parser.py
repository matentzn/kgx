import codecs
from typing import Generator

from rdflib.plugins.parsers.ntriples import W3CNTriplesParser, ParseError
from rdflib.plugins.parsers.ntriples import r_wspace, r_wspaces, r_tail

from kgx.utils.kgx_utils import generate_edge_key


class CustomNTriplesParser(W3CNTriplesParser):
    """
    This class is an extension to ``rdflib.plugins.parsers.ntriples.W3CNTriplesParser``
    that parses N-Triples and yields triples.
    """

    def __init__(self, sink=None):
        W3CNTriplesParser.__init__(self, sink=sink)
        self.file = None
        self.buffer = ""
        self.line = ""
    
    def parse(self, f, bnode_context=None) -> Generator:
        """
        Parses an N-Triples file and yields triples.

        Parameters
        ----------
        f:
            The file-like object to parse
        bnode_context:
            a dict mapping blank node identifiers (e.g., ``a`` in ``_:a``)
            to `~rdflib.term.BNode` instances. An empty dict can be
            passed in to define a distinct context for a given call to `parse`.

        Returns
        -------
        Generator
            A generator for triples

        """
        if not hasattr(f, "read"):
            raise ParseError("Item to parse must be a file-like object.")

        # since N-Triples 1.1 files can and should be utf-8 encoded
        f = codecs.getreader("utf-8")(f)

        self.file = f
        self.buffer = ""
        while True:
            self.line = self.readline()
            if self.line is None:
                break
            if self.line == "":
                raise ParseError(f"Empty line encountered in {str(f)}. "
                                 f"Ensure that no leading or trailing empty lines persist "
                                 f"in the N-Triples file.")
            try:
                yield from self.parseline()
            except ParseError:
                raise ParseError("Invalid line: %r" % self.line)

    @staticmethod
    def _node(node_id):
        # TODO: need to annotate the bare concept node with a precise inferred category
        return [
            node_id,
            {
                "id": node_id,
                "category": ["biolink:NamedThing"]
            }
        ]

    @staticmethod
    def _triple(subject, predicate, object):
        key = generate_edge_key(subject, predicate, object)
        # Edge record format: [s, o, key, edge_data] where edge_data is a dictionary of the edge
        return [
            subject,
            object,
            key,
            {
                "subject": subject,
                "predicate": predicate,
                "object": object
            }
        ]

    def parseline(self, bnode_context=None) -> Generator:
        """
        Parse each line and yield triples.

        Parameters
        ----------
        bnode_context:
            a dict mapping blank node identifiers (e.g., ``a`` in ``_:a``)
            to `~rdflib.term.BNode` instances. An empty dict can be
            passed in to define a distinct context for a given call to `parse`.

        Returns:
        -------
        Generator
            A generator for triples

        """
        # TODO: How would we handle blank nodes here (using bnode_context)?
        if not hasattr(self, 'sink'):
            raise ParseError("CustomNTriplesParser is missing a sink?")

        self.eat(r_wspace)
        
        if self.line and not self.line.startswith("#"):
            
            subject = self.subject()
            
            self.eat(r_wspaces)

            predicate = self.predicate()
            self.eat(r_wspaces)

            object = self.object()
            self.eat(r_tail)

            if self.line:
                raise ParseError("Trailing garbage")

            # Yields a single edge record from one source ntriple line
            yield self._node(subject)
            yield self._node(object)
            yield self._triple(subject, predicate, object)
        else:
            raise StopIteration()
