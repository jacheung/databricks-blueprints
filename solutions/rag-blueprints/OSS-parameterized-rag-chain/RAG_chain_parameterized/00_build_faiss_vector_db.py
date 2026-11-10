# Databricks notebook source
# MAGIC %md
# MAGIC ## Build FAISS Vector DB
# MAGIC Use this script below to build your vector database and save the embeddings + database to DBFS. These two cached items will be used in our RAG solution. 

# COMMAND ----------

# MAGIC %pip install -U --quiet databricks-sdk mlflow-skinny mlflow mlflow[gateway] langchain langchain_core faiss-gpu-cu12 langchain_community transformers langchain_huggingface
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import mlflow
model_config = mlflow.models.ModelConfig(development_config='../rag_chain_config.yaml')

# COMMAND ----------

# Download the embedding model to use for our vector DB
from langchain_huggingface import HuggingFaceEmbeddings

embeddings = HuggingFaceEmbeddings(model_name=model_config.get("faiss_embedding_model"),
                                   cache_folder=model_config.get("faiss_embedding_model_dbfs_cache_dir"))

# COMMAND ----------

# VECTOR_INDEX
import faiss
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_community.vectorstores import FAISS
from uuid import uuid4
from langchain_core.documents import Document


index = faiss.IndexFlatL2(len(embeddings.embed_query("hello world")))

vector_store = FAISS(
    embedding_function=embeddings,
    index=index,
    docstore=InMemoryDocstore(),
    index_to_docstore_id={},
)

document_1 = Document(
    page_content="I had chocalate chip pancakes and scrambled eggs for breakfast this morning.",
    metadata={"source": "tweet"},
)

document_2 = Document(
    page_content="The weather forecast for tomorrow is cloudy and overcast, with a high of 62 degrees.",
    metadata={"source": "news"},
)

document_3 = Document(
    page_content="Building an exciting new project with LangChain - come check it out!",
    metadata={"source": "tweet"},
)

document_4 = Document(
    page_content="Robbers broke into the city bank and stole $1 million in cash.",
    metadata={"source": "news"},
)

document_5 = Document(
    page_content="Wow! That was an amazing movie. I can't wait to see it again.",
    metadata={"source": "tweet"},
)

document_6 = Document(
    page_content="Is the new iPhone worth the price? Read this review to find out.",
    metadata={"source": "website"},
)

document_7 = Document(
    page_content="The top 10 soccer players in the world right now.",
    metadata={"source": "website"},
)

document_8 = Document(
    page_content="LangGraph is the best framework for building stateful, agentic applications!",
    metadata={"source": "tweet"},
)

document_9 = Document(
    page_content="The stock market is down 500 points today due to fears of a recession.",
    metadata={"source": "news"},
)

document_10 = Document(
    page_content="I have a bad feeling I am going to get deleted :(",
    metadata={"source": "tweet"},
)

documents = [
    document_1,
    document_2,
    document_3,
    document_4,
    document_5,
    document_6,
    document_7,
    document_8,
    document_9,
    document_10,
]
uuids = [str(uuid4()) for _ in range(len(documents))]

vector_store.add_documents(documents=documents, ids=uuids)

vector_store.save_local(model_config.get('faiss_dbfs_cache_dir'))

# COMMAND ----------

# test loading the FAISS vector DB and searching
new_vector_store = FAISS.load_local(
    model_config.get('faiss_dbfs_cache_dir'),
     embeddings,
      allow_dangerous_deserialization=True
)

docs = new_vector_store.similarity_search("money")
docs[0]
