from typing import Any, Callable, Dict, List

try:
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import bulk
except ImportError:
    raise ImportError(
        "Elasticsearch requires extra dependencies. Install with `pip install embedchain[elasticsearch]`"
    ) from None

from embedchain.config import ElasticsearchDBConfig
from embedchain.models.VectorDimensions import VectorDimensions
from embedchain.vectordb.base_vector_db import BaseVectorDB


class ElasticsearchDB(BaseVectorDB):
    def __init__(
        self,
        es_config: ElasticsearchDBConfig = None,
        embedding_fn: Callable[[list[str]], list[str]] = None,
        vector_dim: VectorDimensions = None,
        collection_name: str = None,
    ):
        """
        Elasticsearch as vector database
        :param es_config. elasticsearch database config to be used for connection
        :param embedding_fn: Function to generate embedding vectors.
        :param vector_dim: Vector dimension generated by embedding fn
        :param collection_name: Optional. Collection name for the database.
        """
        if not hasattr(embedding_fn, "__call__"):
            raise ValueError("Embedding function is not a function")
        if es_config is None:
            raise ValueError("ElasticsearchDBConfig is required")
        if vector_dim is None:
            raise ValueError("Vector Dimension is required to refer correct index and mapping")
        if collection_name is None:
            raise ValueError("collection name is required. It cannot be empty")
        self.embedding_fn = embedding_fn
        self.client = Elasticsearch(es_config.ES_URL, **es_config.ES_EXTRA_PARAMS)
        self.vector_dim = vector_dim
        self.es_index = f"{collection_name}_{self.vector_dim}"
        index_settings = {
            "mappings": {
                "properties": {
                    "text": {"type": "text"},
                    "embeddings": {"type": "dense_vector", "index": False, "dims": self.vector_dim},
                }
            }
        }
        if not self.client.indices.exists(index=self.es_index):
            # create index if not exist
            print("Creating index", self.es_index, index_settings)
            self.client.indices.create(index=self.es_index, body=index_settings)
        super().__init__()

    def _get_or_create_db(self):
        return self.client

    def _get_or_create_collection(self, name):
        """Note: nothing to return here. Discuss later"""

    def get(self, ids: List[str], where: Dict[str, any]) -> List[str]:
        """
        Get existing doc ids present in vector database
        :param ids: list of doc ids to check for existance
        :param where: Optional. to filter data
        """
        query = {"bool": {"must": [{"ids": {"values": ids}}]}}
        if "app_id" in where:
            app_id = where["app_id"]
            query["bool"]["must"].append({"term": {"metadata.app_id": app_id}})
        response = self.client.search(index=self.es_index, query=query, _source=False)
        docs = response["hits"]["hits"]
        ids = [doc["_id"] for doc in docs]
        return set(ids)

    def add(self, documents: List[str], metadatas: List[object], ids: List[str]) -> Any:
        """
        add data in vector database
        :param documents: list of texts to add
        :param metadatas: list of metadata associated with docs
        :param ids: ids of docs
        """
        embeddings = self.embedding_fn(documents)
        docs = [
            {
                "_index": self.es_index,
                "_id": id,
                "_source": {
                    "text": text,
                    "metadata": metadata,
                    "embeddings": embeddings,
                },
            }
            for id, text, metadata, embeddings in zip(
                ids, documents, metadatas, embeddings
            )
        ]
        bulk(self.client, docs)
        self.client.indices.refresh(index=self.es_index)
        return

    def query(self, input_query: List[str], n_results: int, where: Dict[str, any]) -> List[str]:
        """
        query contents from vector data base based on vector similarity
        :param input_query: list of query string
        :param n_results: no of similar documents to fetch from database
        :param where: Optional. to filter data
        """
        input_query_vector = self.embedding_fn(input_query)
        query_vector = input_query_vector[0]
        query = {
            "script_score": {
                "query": {"bool": {"must": [{"exists": {"field": "text"}}]}},
                "script": {
                    "source": "cosineSimilarity(params.input_query_vector, 'embeddings') + 1.0",
                    "params": {"input_query_vector": query_vector},
                },
            }
        }
        if "app_id" in where:
            app_id = where["app_id"]
            query["script_score"]["query"]["bool"]["must"] = [{"term": {"metadata.app_id": app_id}}]
        _source = ["text"]
        response = self.client.search(index=self.es_index, query=query, _source=_source, size=n_results)
        docs = response["hits"]["hits"]
        return [doc["_source"]["text"] for doc in docs]

    def count(self) -> int:
        query = {"match_all": {}}
        response = self.client.count(index=self.es_index, query=query)
        return response["count"]

    def reset(self):
        # Delete all data from the database
        if self.client.indices.exists(index=self.es_index):
            # delete index in Es
            self.client.indices.delete(index=self.es_index)
