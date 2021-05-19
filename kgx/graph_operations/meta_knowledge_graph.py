from typing import Dict, List, Optional, Any, Callable
from sys import stderr

import yaml
from json import dump
from json.encoder import JSONEncoder

from kgx import GraphEntityType
from kgx.prefix_manager import PrefixManager
from kgx.graph.base_graph import BaseGraph

"""
Generate a knowledge map that corresponds to TRAPI KnowledgeMap.
Specification based on TRAPI Draft PR: https://github.com/NCATSTranslator/ReasonerAPI/pull/171
"""


####################################################################
# Next Generation Implementation of Graph Summary coding which
# leverages the new "Transformer.process()" data stream "Inspector"
# design pattern, implemented here as a "Callable" inspection class.
####################################################################
def mkg_default(o):
    """
    JSONEncoder 'default' function override to
    properly serialize 'Set' objects (into 'List')
    """
    if isinstance(o, MetaKnowledgeGraph.Category):
        return o.json_object()
    else:
        try:
            iterable = iter(o)
        except TypeError:
            pass
        else:
            return list(iterable)
        # Let the base class default method raise the TypeError
        return JSONEncoder.default(o)


class MetaKnowledgeGraph:
    """
    Class for generating a TRAPI 1.1 style of "meta knowledge graph" summary.

    The optional 'progress_monitor' for the validator should be a lightweight Callable
    which is injected into the class 'inspector' Callable, designed to intercepts
    node and edge records streaming through the Validator (inside a Transformer.process() call.
    The first (GraphEntityType) argument of the Callable tags the record as a NODE or an EDGE.
    The second argument given to the Callable is the current record itself.
    This Callable is strictly meant to be procedural and should *not* mutate the record.
    The intent of this Callable is to provide a hook to KGX applications wanting the
    namesake function of passively monitoring the graph data stream. As such, the Callable
    could simply tally up the number of times it is called with a NODE or an EDGE, then
    provide a suitable (quick!) report of that count back to the KGX application. The
    Callable (function/callable class) should not modify the record and should be of low
    complexity, so as not to introduce a large computational overhead to validation!

    Parameters
    ----------
    name: str
        (Graph) name assigned to the summary.
    progress_monitor: Optional[Callable[[GraphEntityType, List], None]]
        Function given a peek at the current record being processed by the class wrapped Callable.
    error_log:
        Where to write any graph processing error message (stderr, by default)
    """
    def __init__(
            self,
            name='',
            progress_monitor: Optional[Callable[[GraphEntityType, List], None]] = None,
            error_log=None,
            **kwargs
    ):
        # formal args

        self.name = name
        self.progress_monitor: Optional[Callable[[GraphEntityType, List], None]] = progress_monitor

        # internal attributes
        self.node_catalog: Dict[str, List[int]] = dict()

        self.node_stats: Dict[str, MetaKnowledgeGraph.Category] = dict()
        self.node_stats['unknown'] = self.Category('unknown')

        self.edge_record_count: int = 0
        self.predicates: Dict = dict()
        self.association_map: Dict = dict()
        self.edge_stats = []
        self.graph_stats: Dict[str, Dict] = dict()

        if error_log:
            self.error_log = open(error_log, 'w')
        else:
            self.error_log = stderr

    def __call__(self, entity_type: GraphEntityType, rec: List):
        """
        Transformer 'inspector' Callable
        """
        if self.progress_monitor:
            self.progress_monitor(entity_type, rec)
        if entity_type == GraphEntityType.EDGE:
            self.analyse_edge(*rec)
        elif entity_type == GraphEntityType.NODE:
            self.analyse_node(*rec)
        else:
            raise RuntimeError("Unexpected GraphEntityType: " + str(entity_type))

    class Category:
        # The 'category map' just associates a unique int catalog
        # index ('cid') value as a proxy for the full curie string,
        # to reduce storage in the main node catalog
        _category_curie_map: List[str] = list()

        def __init__(self, category=''):
            self.category = category
            if category not in self._category_curie_map:
                self._category_curie_map.append(category)
            self.category_stats: Dict[str, Any] = dict()
            self.category_stats['id_prefixes'] = set()
            self.category_stats['count'] = 0
            self.category_stats['count_by_source'] = {'unknown': 0}

        def get_cid(self):
            return self._category_curie_map.index(self.category)

        @classmethod
        def get_category_curie(cls, cid: int):
            return cls._category_curie_map[cid]

        def get_id_prefixes(self):
            return self.category_stats['id_prefixes']

        def get_count(self):
            return self.category_stats['count']

        def get_count_by_source(self, source: str = None) -> Dict:
            if source:
                return {source: self.category_stats['count_by_source'][source]}
            return self.category_stats['count_by_source']

        def analyse_node_category(self, n, data):
            self.category_stats['count'] += 1
            prefix = PrefixManager.get_prefix(n)
            if not prefix:
                print(f"Warning: node id {n} has no CURIE prefix", file=self.error_log)
            else:
                if prefix not in self.category_stats['id_prefixes']:
                    self.category_stats['id_prefixes'].add(prefix)
            if 'provided_by' in data:
                for s in data['provided_by']:
                    if s in self.category_stats['count_by_source']:
                        self.category_stats['count_by_source'][s] += 1
                    else:
                        self.category_stats['count_by_source'][s] = 1
            else:
                self.category_stats['count_by_source']['unknown'] += 1

        def json_object(self):
            return {
                'id_prefixes': list(self.category_stats['id_prefixes']),
                'count': self.category_stats['count'],
                'count_by_source': self.category_stats['count_by_source']
            }

    def analyse_node(self, n, data):
        # The TRAPI release 1.1 meta_knowledge_graph format indexes nodes by biolink:Category
        # the node 'category' field is a list of assigned categories (usually just one...).
        # However, this may perhaps sometimes result in duplicate counting and conflation of prefixes(?).
        if n in self.node_catalog:
            # Report duplications of node records, as discerned from node id.
            print("Duplicate node identifier '" + n +
                  "' encountered in input node data? Ignoring...", file=self.error_log)
            return
        else:
            self.node_catalog[n] = list()
            
        if 'category' not in data:
            category = self.node_stats['unknown']
            category.analyse_node_category(n, data)
            print(
                "Node with identifier '" + n + "' is missing its 'category' value? " +
                "Counting it as 'unknown', but otherwise ignoring in the analysis...", file=self.error_log
            )
            return

        categories = data['category']

        for category_data in categories:
            # we note here that category_curie may be
            # a piped '|' set of Biolink category CURIE values
            categories = category_data.split("|")
            # analyse them each independently...
            for category_curie in categories:
                if category_curie not in self.node_stats:
                    self.node_stats[category_curie] = self.Category(category_curie)
                category = self.node_stats[category_curie]
                category_idx: int = category.get_cid()
                if category_idx not in self.node_catalog[n]:
                    self.node_catalog[n].append(category_idx)
                category.analyse_node_category(n, data)

    def analyse_edge(self, u, v, k, data):
        # we blissfully assume that all the nodes of a
        # graph stream were analysed first by the MetaKnowledgeGraph
        # before the edges are analysed, thus we can test for
        # node 'n' existence internally, by identifier.
        #
        # Given the use case of multiple categories being assigned to a given node in a KGX data file,
        # either by category inheritance (ancestry all the way back up to NamedThing)
        # or by conflation (i.e. gene == protein id?), then the Cartesian product of
        # subject/object edges mappings need to be captured here.
        #
        self.edge_record_count += 1

        predicate = data['predicate']
        if predicate not in self.predicates:
            # just need to track the number
            # of edge records using this predicate
            self.predicates[predicate] = 0
        self.predicates[predicate] += 1

        if u not in self.node_catalog:
            print("Edge 'subject' node ID '" + u + "' not found in node catalog? Ignoring...", file=self.error_log)
            # removing from edge count
            self.edge_record_count -= 1
            self.predicates[predicate] -= 1
            return
        else:
            for subj_cat_idx in self.node_catalog[u]:
                subject_category = MetaKnowledgeGraph.Category.get_category_curie(subj_cat_idx)

                if v not in self.node_catalog:
                    print("Edge 'object' node ID '" + v +
                          "' not found in node catalog? Ignoring...", file=self.error_log)
                    self.edge_record_count -= 1
                    self.predicates[predicate] -= 1
                    return
                else:
                    for obj_cat_idx in self.node_catalog[v]:
                        object_category = MetaKnowledgeGraph.Category.get_category_curie(obj_cat_idx)

                        # Process the 'valid' S-P-O triple here...
                        triple = (subject_category, predicate, object_category)
                        if triple not in self.association_map:
                            self.association_map[triple] = {
                                'subject': triple[0],
                                'predicate': triple[1],
                                'object': triple[2],
                                'relations': set(),
                                'count': 0,
                                'count_by_source': {'unknown': 0},
                            }

                        if data['relation'] not in self.association_map[triple]['relations']:
                            self.association_map[triple]['relations'].add(data['relation'])

                        self.association_map[triple]['count'] += 1
                        if 'provided_by' in data:
                            for s in data['provided_by']:
                                if s not in self.association_map[triple]['count_by_source']:
                                    self.association_map[triple]['count_by_source'][s] = 1
                                else:
                                    self.association_map[triple]['count_by_source'][s] += 1
                        else:
                            self.association_map[triple]['count_by_source']['unknown'] += 1

    def get_name(self):
        """
        Returns
        -------
        str
            Currently assigned knowledge graph name.
        """
        return self.name

    def get_category(self, category_curie: str) -> Category:
        """
        Counts the number of distinct (Biolink) categories encountered
        in the knowledge graph (not including those of 'unknown' category)

        Parameters
        ----------
        category_curie: str
            Curie identifier for the (Biolink) category.

        Returns
        -------
        Category
            MetaKnowledgeGraph.Category object for a given Biolink category.
        """
        return self.node_stats[category_curie]

    def get_node_stats(self) -> Dict[str, Category]:
        if 'unknown' in self.node_stats and not self.node_stats['unknown'].get_count():
            self.node_stats.pop('unknown')
        return self.node_stats

    def get_number_of_categories(self) -> int:
        """
        Counts the number of distinct (Biolink) categories encountered
        in the knowledge graph (not including those of 'unknown' category)

        Returns
        -------
        int
            Number of distinct (Biolink) categories found in the graph (excluding the 'unknown' category)
        """
        return len([c for c in self.node_stats.keys() if c != 'unknown'])

    def get_edge_stats(self) -> List:
        # Not sure if this is "safe" but assume
        # that edge_stats may be cached once computed?
        if not self.edge_stats:
            for k, v in self.association_map.items():
                kedge = v
                relations = list(v['relations'])
                kedge['relations'] = relations
                self.edge_stats.append(kedge)
        return self.edge_stats

    def get_total_nodes_count(self) -> int:
        """
        Counts the total number of distinct nodes in the knowledge graph
        (**not** including those ignored due to being of 'unknown' category)

        Returns
        -------
        int
            Number of distinct nodes in the knowledge.
        """
        return len(self.node_catalog)

    def get_node_count_by_category(self, category_curie: str) -> int:
        """
        Counts the number of edges in the graph
        with the specified (Biolink) category curie.

        Parameters
        ----------
        category_curie: str
            Curie identifier for the (Biolink) category.

        Returns
        -------
        int
            Number of nodes for the given category.

        Raises
        ------
        RuntimeError
            Error if category identifier is empty string or None.
        """
        if not category_curie:
            raise RuntimeError("get_node_count_by_category(): null or empty category argument!?")
        if category_curie in self.node_stats.keys():
            return self.node_stats[category_curie].get_count()
        else:
            return 0

    def get_total_node_counts_across_categories(self) -> int:
        """
        The aggregate count of all node category assignments for every category.
        Note that nodes with multiple categories will have their count replicated
        under each of its categories.

        Parameters
        ----------
        category_curie: str
            Curie identifier for the (Biolink) category.

        Returns
        -------
        int
            Number of nodes for the given category.
        """
        count = 0
        for category in self.node_stats.values():
            count += category.get_count()
        return count

    def get_total_edges_count(self) -> int:
        """
        Gets the total number of 'valid' edges in the data set
        (ignoring those with 'unknown' subject or predicate category mappings)

        :return int count of edges
        """
        return self.edge_record_count

    def get_edge_mapping_count(self) -> int:
        """
        Counts the number of distinct edge
        Subject (category) - P (predicate) -> Object (category)
        mappings in the knowledge graph.

        Returns
        ----------
        int
            Count of mappings
        """
        return len(self.get_edge_stats())

    def get_predicate_count(self) -> int:
        """
        Counts the number of distinct edge predicates
        in the knowledge graph.

        Returns
        ----------
        int
            Number of (Biolink) predicates.
        """
        return len(self.predicates)

    def get_edge_count_by_predicate(self, predicate_curie: str) -> int:
        """
        Counts the number of edges in the graph with the specified predicate.

        Parameters
        ----------
        predicate_curie: str
            (Biolink) curie identifier for the predicate.

        Returns
        -------
        int
            Number of edges for the given predicate.

        Raises
        ------
        RuntimeError
            Error if predicate identifier is empty string or None.
        """
        if not predicate_curie:
            raise RuntimeError("get_node_count_by_category(): null or empty predicate argument!?")
        if predicate_curie in self.predicates:
            return self.predicates[predicate_curie]
        return 0

    def get_total_edge_counts_across_mappings(self) -> int:
        """
        Aggregate count of the edges in the graph for every mapping. Edges
        with subject and object nodes with multiple assigned categories will
        have their count replicated under each distinct mapping of its categories.

        Returns
        -------
        int
            Number of the edges counted across all mappings.
        """
        count = 0
        for edge in self.get_edge_stats():
            count += edge['count']
        return count

    def summarize_graph_nodes(self, graph: BaseGraph) -> Dict:
        """
        Summarize the nodes in a graph.

        Parameters
        ----------
        graph: kgx.graph.base_graph.BaseGraph
            The graph

        Returns
        -------
        Dict
            The node stats
        """
        for n, data in graph.nodes(data=True):
            self.analyse_node(n, data)
        return self.get_node_stats()

    def summarize_graph_edges(self, graph: BaseGraph) -> List[Dict]:
        """
        Summarize the edges in a graph.

        Parameters
        ----------
        graph: kgx.graph.base_graph.BaseGraph
            The graph

        Returns
        -------
        List[Dict]
            The edge stats

        """
        for u, v, k, data in graph.edges(keys=True, data=True):
            self.analyse_edge(u, v, k, data)
        return self.get_edge_stats()

    def summarize_graph(
            self,
            graph: BaseGraph,
            name: str = None,
            **kwargs
    ) -> Dict:
        """
        Generate a meta knowledge graph that describes the composition of the graph.

        Parameters
        ----------
        graph: kgx.graph.base_graph.BaseGraph
            The graph
        name: Optional[str]
            Name for the graph
        kwargs: Dict
            Any additional arguments (ignored in this method at present)

        Returns
        -------
        Dict
            A knowledge map dictionary corresponding to the graph
        """
        if not self.graph_stats:
            node_stats = self.summarize_graph_nodes(graph)
            edge_stats = self.summarize_graph_edges(graph)
            # JSON sent back as TRAPI 1.1 version,
            # without the global 'knowledge_map' object tag
            self.graph_stats = {
                'nodes': node_stats,
                'edges': edge_stats
            }
            if name:
                self.graph_stats['name'] = name
            else:
                self.graph_stats['name'] = self.name
        return self.graph_stats
    
    def get_graph_summary(self, name: str = None, **kwargs) -> Dict:
        """
        Similar to summarize_graph except that the node and edge statistics are already captured
        in the MetaKnowledgeGraph class instance (perhaps by Transformer.process() stream inspection)
        and therefore, the data structure simply needs to be 'finalized' for saving or similar use.

        Parameters
        ----------
        name: Optional[str]
            Name for the graph (if being renamed)
        kwargs: Dict
            Any additional arguments (ignored in this method at present)

        Returns
        -------
        Dict
            A knowledge map dictionary corresponding to the graph

        """
        if not self.graph_stats:
            # JSON sent back as TRAPI 1.1 version,
            # without the global 'knowledge_map' object tag
            self.graph_stats = {
                'nodes': self.get_node_stats(),
                'edges': self.get_edge_stats()
            }
            if name:
                self.graph_stats['name'] = name
            else:
                self.graph_stats['name'] = self.name
        return self.graph_stats

    def save(self, file, name: str = None, file_format: str = 'json'):
        """
        Save the current MetaKnowledgeGraph to a specified (open) file (device)
        """
        stats = self.get_graph_summary(name)
        if not file_format or file_format == 'json':
            dump(stats, file, indent=4, default=mkg_default)
        else:
            yaml.dump(stats, file)


def generate_meta_knowledge_graph(graph: BaseGraph, name: str, filename: str) -> None:
    """
    Generate a knowledge map that describes the composition of the graph
    and write to ``filename``.

    Parameters
    ----------
    graph: kgx.graph.base_graph.BaseGraph
        The graph
    name: Optional[str]
        Name for the graph
    filename: str
        The file to write the knowledge map to

    """
    graph_stats = summarize_graph(graph, name)
    with open(filename, mode='w') as mkgh:
        dump(graph_stats, mkgh, indent=4, default=mkg_default)


def summarize_graph(graph: BaseGraph, name: str = None, **kwargs) -> Dict:
    """
    Generate a meta knowledge graph that describes the composition of the graph.

    Parameters
    ----------
    graph: kgx.graph.base_graph.BaseGraph
        The graph
    name: Optional[str]
        Name for the graph
    kwargs: Dict
        Any additional arguments

    Returns
    -------
    Dict
        A knowledge map dictionary corresponding to the graph

    """
    mkg = MetaKnowledgeGraph(name)
    return mkg.summarize_graph(graph)