# Databricks notebook source
# MAGIC
# MAGIC %pip install -U --quiet databricks-sdk==0.28.0 databricks-agents mlflow-skinny mlflow mlflow[gateway] langchain_core langchain_community langgraph langgraph-checkpoint-sqlite langchain-databricks
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import getpass
import os
if "DATABRICKS_TOKEN" not in os.environ:
  dbutils.secrets.get('notebooks', 'databricks_token')

# COMMAND ----------

from langchain_core.tools import tool


@tool
def multiply(first_int: int, second_int: int) -> int:
    """Multiply two integers together."""
    return first_int * second_int

@tool
def add(first_int: int, second_int: int) -> int:
    "Add two integers."
    return first_int + second_int

@tool
def exponentiate(base: int, exponent: int) -> int:
    "Exponentiate the base to the exponent power."
    return base**exponent

# COMMAND ----------

# Import relevant functionality
from langchain_databricks import ChatDatabricks
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

# Create the ReAct Agent
# In ReACT, an LLM is given a prompt describing tools it has access to and a scratch pad for dumping intermediate step results.
memory = MemorySaver() # Memory with summary
model = ChatDatabricks(
    endpoint="databricks-meta-llama-3-1-70b-instruct",
    temperature=0.1)
tools = [multiply, add, exponentiate]
agent_executor = create_react_agent(model, tools)
response = agent_executor.invoke({"messages": [HumanMessage(content="what is 1023 * 12390. After that, add the number 8 to it.")]})

response["messages"]


# COMMAND ----------



# COMMAND ----------

from IPython.display import Image, display

display(Image(agent_executor.get_graph().draw_mermaid_png()))
