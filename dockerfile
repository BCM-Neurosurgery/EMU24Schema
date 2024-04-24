FROM python:3.9-slim-buster
#changed to 11.7.1 from 11.7.0 PJS 2024-01-04

ARG PYTHON_VERSION=3.9
WORKDIR /app
RUN apt-get update && apt-get install -y python3-tk

RUN apt-get update && apt-get install -y curl

# Install graphviz
RUN apt-get update && apt-get install -y graphviz

# Add graphviz to the system path for all users
RUN echo 'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/share/graphviz"' >> /etc/environment

# Install any needed packages specified in requirements.txt
COPY requirements.txt .
ENV PATH /opt/conda/bin:$PATH 
RUN pip install --trusted-host pypi.python.org -r requirements.txt 



ENV DJ_SUPPORT_FILEPATH_MANAGEMENT=TRUE


CMD ["pip", "install", "/app/Dependencies/Blackrock-Python-Utilities", "/app/Dependencies/pyNsXStitch"] 