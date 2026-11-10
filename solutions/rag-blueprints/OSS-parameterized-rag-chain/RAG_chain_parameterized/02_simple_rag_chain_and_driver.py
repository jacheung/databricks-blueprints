# Databricks notebook source
# MAGIC %md 
# MAGIC ## Simple RAG Chain and Driver
# MAGIC This notebook leverages a completions model endpoint (e.g. Zephyr-7b-beta) for RAG using a baked in FAISS Vector Database. 
# MAGIC
# MAGIC Note that one of the disadvantages of this solution is that updating the vector database necessitates a redeployment of the serving container. Furthermore, as the size of the vector database grows, the size of the container will have to too. Why? MLflow `pyfunc` lets us copy external files into the artifacts folder, to be used in deployment. This lets us serve a FAISS database with our RAG solution. Switching to Unity Catalog in the future and **leveraging Vector Index Endpoints solves this problem by abstracting out the Retrieval** (i.e. vector database) part of RAG. 

# COMMAND ----------

# MAGIC %pip install -U --quiet databricks-sdk mlflow-skinny mlflow mlflow[gateway] langchain langchain_core faiss-gpu-cu12 langchain_community transformers langchain_huggingface
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %%writefile ragchain-mlflow.py
# MAGIC import os
# MAGIC import mlflow
# MAGIC
# MAGIC os.environ['PYDANTIC_SKIP_VALIDATING_CORE_SCHEMAS']='1'
# MAGIC
# MAGIC class RAGChainModel(mlflow.pyfunc.PythonModel):
# MAGIC     def __init__(self, llm_model_serving_endpoint_name='lol-zephyr-7b-beta'):
# MAGIC         from langchain_community.chat_models import ChatDatabricks
# MAGIC         # completions model serving endpoint
# MAGIC         self.llm = ChatDatabricks(endpoint=llm_model_serving_endpoint_name)
# MAGIC
# MAGIC     def load_context(self, context):
# MAGIC         from langchain.vectorstores import FAISS
# MAGIC         from langchain_core.output_parsers import StrOutputParser
# MAGIC         from langchain import hub
# MAGIC         from langchain_core.runnables import RunnablePassthrough
# MAGIC         from langchain_huggingface import HuggingFaceEmbeddings
# MAGIC
# MAGIC         def format_docs(docs):
# MAGIC             return "\n\n".join([d.page_content for d in docs])
# MAGIC
# MAGIC         # embedding model with vector database
# MAGIC         faiss_embedding_model='sentence-transformers/all-MiniLM-L6-v2'
# MAGIC         embeddings=HuggingFaceEmbeddings(model_name=faiss_embedding_model,
# MAGIC                                          cache_folder=context.artifacts['embeddings_model_cache_dir'])
# MAGIC         faiss_index = FAISS.load_local(context.artifacts['faiss_dbfs_cache_dir'],
# MAGIC                                        embeddings,
# MAGIC                                        allow_dangerous_deserialization=True)
# MAGIC         self.retriever = faiss_index.as_retriever()
# MAGIC
# MAGIC         # load in prompt from HF hub
# MAGIC         self.prompt = hub.pull("rlm/rag-prompt-mistral")
# MAGIC         
# MAGIC         # define complete chain
# MAGIC         self.rag_chain = (
# MAGIC             {"context": self.retriever | format_docs, "question": RunnablePassthrough()}
# MAGIC             | self.prompt
# MAGIC             | self.llm
# MAGIC             | StrOutputParser()
# MAGIC         )
# MAGIC
# MAGIC     def process_row(self, row):
# MAGIC        return self.rag_chain.invoke(row['prompt'])
# MAGIC     
# MAGIC     def predict(self, context, data):
# MAGIC         results = data.apply(self.process_row, axis=1) 
# MAGIC         results_text = results.apply(lambda x: x)
# MAGIC         return results_text
# MAGIC
# MAGIC mlflow.models.set_model(RAGChainModel())
# MAGIC

# COMMAND ----------

from pprint import pprint
import os
import mlflow

registered_model_name = 'lol-rag-chain-pyfunc'

artifact_dict = {
  'embeddings_model_cache_dir': '/dbfs/Users/jon.cheung@databricks.com/sasb/serialization/all-MiniLM-L6-v2',
  'faiss_dbfs_cache_dir': '/dbfs/Users/jon.cheung@databricks.com/sasb/serialization-sample/faiss_index'
}

input_example = {"messages": 
  [ 
   {"role": "user",
     "content": "What is Retrieval-augmented Generation?"
     }
   ]
  }

chain_path = "ragchain-mlflow.py"

# this is used to force my UC-enabled registry to swap to Workspace-managed registry
mlflow.set_registry_uri('databricks')

with mlflow.start_run():
    logged_model = mlflow.pyfunc.log_model(python_model=chain_path,
                                           artifact_path='ragchain-mlflow', 
                                           artifacts=artifact_dict, 
                                           registered_model_name=registered_model_name,
                                           input_example=input_example
                                           )

# COMMAND ----------

# test loading the registered model
model = mlflow.pyfunc.load_model(logged_model.model_uri)

# COMMAND ----------

# test inferencing on the registered model
import pandas as pd

df = pd.DataFrame({'prompt': ['What is the stock market like today?']})

model.predict(df)

# COMMAND ----------

# MAGIC %md 
# MAGIC ## Serve the model using the MLflow API SDK
# MAGIC
# MAGIC Feel free to use the UI as well. You can go to your registered model and select "Use Model For Inference" at the top right. 

# COMMAND ----------

from mlflow.deployments import get_deploy_client

endpoint_name = 'lol-rag-chain-endpoint'
registered_model_name = 'lol-rag-chain-pyfunc'
registered_models_info = mlflow.search_registered_models(filter_string=f"name='{registered_model_name}'")

client = get_deploy_client("databricks")
endpoint = client.create_endpoint(
    name=endpoint_name,
    config={
        "served_entities": [
            {
                "name": registered_model_name,
                "entity_name": registered_model_name,
                "entity_version": registered_models_info[0].latest_versions[0].version,
                "workload_size": "Medium",
                "scale_to_zero_enabled": True
            }
        ],
        "traffic_config": {
            "routes": [
                {
                    "served_model_name": registered_model_name,
                    "traffic_percentage": 100
                }
            ]
        }
    }
)



# COMMAND ----------

# MAGIC %md
# MAGIC ## BONUS SECTION: Logging model using the langchain model flavor
# MAGIC
# MAGIC Currently we log with the `pyfunc` model flavor above. This gives us the benefit of attaching artifacts that can be used by the inference model (e.g. the FAISS vector database). However, this method of logging doesn't give us visibility into how the chain is operating. 
# MAGIC
# MAGIC Logging the model as a `langchain` model flavor gives us this benefit. We get to see each item that's passed to the next component and the run time for each part of the chain as well. This helps us diagnose bottlenecks. However, one downside is that this mlflow flavor does not allow us to attach artifacts, which is unfortunate for our use-case. Instead, it looks like you have to build the vector DB using a temp directory so that mlflow picks it up. See [here](https://github.com/mlflow/mlflow/blob/master/examples/langchain/retrieval_qa_chain.py). Once you have UC-enabled and are able to utilize Vector Endpoints, we can revisit the below for improved auditability. 

# COMMAND ----------

# # There is a current bug with logging the model config into an mlflow model. I'm investigating. In the meantime we'll have to hard-code some of the parameters before logging.

# # model_config = mlflow.models.ModelConfig(development_config='../rag_chain_config.yaml')
# faiss_embedding_model='sentence-transformers/all-MiniLM-L6-v2'
# faiss_embedding_model_dbfs_cache_dir = '/dbfs/Users/jon.cheung@databricks.com/sasb/serialization/all-MiniLM-L6-v2'
# faiss_dbfs_cache_dir='/dbfs/Users/jon.cheung@databricks.com/sasb/serialization-sample/faiss_index'
# registered_model_name='lol-rag-chain-faiss'
# llm_model_serving_endpoint_name='lol-zephyr-7b-beta'

# COMMAND ----------

# import os
# from langchain.vectorstores import FAISS
# from transformers import AutoTokenizer, AutoModelForCausalLM
# from langchain_core.output_parsers import StrOutputParser
# from langchain import hub
# from langchain_core.runnables import RunnablePassthrough, RunnableParallel
# from langchain_huggingface import HuggingFaceEmbeddings
# from langchain_community.chat_models import ChatDatabricks
# import mlflow

# mlflow.set_registry_uri('databricks')

# def load_retriever(persist_directory):
#   embeddings = HuggingFaceEmbeddings(model_name=faiss_embedding_model,
#                                       cache_folder=faiss_embedding_model_dbfs_cache_dir )
                                   
#   faiss_index = FAISS.load_local(faiss_dbfs_cache_dir,
#                                       embeddings, 
#                                       allow_dangerous_deserialization=True)
#   return faiss_index.as_retriever()


# retriever = load_retriever(faiss_dbfs_cache_dir)
# prompt = hub.pull("rlm/rag-prompt-mistral")
# llm = ChatDatabricks(endpoint=llm_model_serving_endpoint_name)


# # use this line when when we use a separate driver notebook driver to log and deploy the chain
# # you won't have to use mlflow.start_run(). You just have to instantiate the chain then set the model. 
# # mlflow.set_model(model=rag_chain_from_docs)

# with mlflow.start_run() as run:
#     def format_docs(docs):
#       return "\n\n".join(doc.page_content for doc in docs)

#     rag_chain_from_docs = (
#         {"context": retriever | format_docs, "question": RunnablePassthrough()}
#         | prompt
#         | llm
#         | StrOutputParser()
#     )

#     model_info = mlflow.langchain.log_model(
#         lc_model=rag_chain_from_docs,
#         artifact_path="retrieval_qa",
#         loader_fn=load_retriever, # A function that’s required for models containing objects that aren’t natively serialized by LangChain. This function takes a string persist_dir as an argument and returns the specific object that the model needs. This is for our retriever. 
#         persist_dir=faiss_dbfs_cache_dir,
#         registered_model_name=registered_model_name
#       )
