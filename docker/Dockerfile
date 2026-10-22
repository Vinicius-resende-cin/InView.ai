# Builds and runs the conflict-static-analysis tool in the environment of the
# tool's own docker/Dockerfile (Ubuntu 22.04 + OpenJDK 8 + Maven 3.8.8): on Java 8
# the tool reads the JDK from rt.jar and joins classpaths with ':', which is what
# it was written and used for in the study. The tool's Dockerfile also clones the
# whole dataset and miningframework, which isn't needed to build or run it, so
# this image keeps only the toolchain. The tool's source, the dataset and the
# working directories are mounted when a container runs.
FROM ubuntu:22.04

RUN apt-get update --fix-missing && \
    apt-get install -y --no-install-recommends openjdk-8-jdk-headless curl unzip ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME /usr/lib/jvm/java-8-openjdk-amd64/

# Maven, as in the tool's Dockerfile (same version; archive.apache.org because
# dlcdn.apache.org drops old releases). The SHA below is Apache's published
# checksum for this tarball: the one declared in the tool's Dockerfile doesn't
# match the file, which went unnoticed because that Dockerfile never checks it.
ARG MAVEN_VERSION=3.8.8
ARG SHA=332088670d14fa9ff346e6858ca0acca304666596fec86eea89253bd496d3c90deae2be5091be199f48e09d46cec817c6419d5161fb4ee37871503f472765d00
RUN mkdir -p /usr/share/maven && \
    curl -fsSL -o /tmp/apache-maven.tar.gz https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz && \
    echo "${SHA}  /tmp/apache-maven.tar.gz" | sha512sum -c - && \
    tar -xzf /tmp/apache-maven.tar.gz -C /usr/share/maven --strip-components=1 && \
    rm -f /tmp/apache-maven.tar.gz && ln -s /usr/share/maven/bin/mvn /usr/bin/mvn

WORKDIR /work
