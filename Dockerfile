FROM python:3.4

ENV NYUKI_PATH /home

ADD . ${NYUKI_PATH}/
RUN pip3 install -e ${NYUKI_PATH}/
RUN pip3 install -r ${NYUKI_PATH}/requirements_test.txt

EXPOSE 8081
WORKDIR ${NYUKI_PATH}/examples
CMD python3.4 sample.py -a 0.0.0.0:8081 -s prosody:5222 -d -c sample.json