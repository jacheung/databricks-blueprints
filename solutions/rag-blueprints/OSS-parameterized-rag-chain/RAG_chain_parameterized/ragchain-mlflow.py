import os
import mlflow

os.environ['PYDANTIC_SKIP_VALIDATING_CORE_SCHEMAS']='1'

class RAGChainModel(mlflow.pyfunc.PythonModel):
    def __init__(self, llm_model_serving_endpoint_name='lol-zephyr-7b-beta'):
        from langchain_community.chat_models import ChatDatabricks
        # completions model serving endpoint
        self.llm = ChatDatabricks(endpoint=llm_model_serving_endpoint_name)

    def load_context(self, context):
        from langchain.vectorstores import FAISS
        from langchain_core.output_parsers import StrOutputParser
        from langchain import hub
        from langchain_core.runnables import RunnablePassthrough
        from langchain_huggingface import HuggingFaceEmbeddings

        def format_docs(docs):
            return "\n\n".join([d.page_content for d in docs])

        # embedding model with vector database
        faiss_embedding_model='sentence-transformers/all-MiniLM-L6-v2'
        embeddings=HuggingFaceEmbeddings(model_name=faiss_embedding_model,
                                         cache_folder=context.artifacts['embeddings_model_cache_dir'])
        faiss_index = FAISS.load_local(context.artifacts['faiss_dbfs_cache_dir'],
                                       embeddings,
                                       allow_dangerous_deserialization=True)
        self.retriever = faiss_index.as_retriever()

        # load in prompt from HF hub
        self.prompt = hub.pull("rlm/rag-prompt-mistral")
        
        # define complete chain
        self.rag_chain = (
            {"context": self.retriever | format_docs, "question": RunnablePassthrough()}
            | self.prompt
            | self.llm
            | StrOutputParser()
        )

    def process_row(self, row):
       return self.rag_chain.invoke(row['prompt'])
    
    def predict(self, context, data):
        results = data.apply(self.process_row, axis=1) 
        results_text = results.apply(lambda x: x)
        return results_text
    
    # def predict(self, context, model_input: str): 
        
    #     return self.rag_chain.invoke(model_input)


mlflow.models.set_model(RAGChainModel())
