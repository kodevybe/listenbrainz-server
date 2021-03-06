import os
import logging
from listenbrainz.bigquery import create_bigquery_object
import time
from listenbrainz import default_config as config
try:
    from listenbrainz import custom_config as config
except ImportError:
    pass

JOB_COMPLETION_CHECK_DELAY = 5 # seconds

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

bigquery = None


def init_bigquery_connection():
    """ Initiates the connection to Google BigQuery """

    global bigquery
    bigquery = create_bigquery_object()


def get_parameters_dict(parameters):
    """ Converts a list of parameters to be passed to BigQuery into the standard format that the API requires.
        The format can be seen here:
        https://developers.google.com/resources/api-libraries/documentation/bigquery/v2/python/latest/bigquery_v2.jobs.html#query

        Args: parameters: a list of dictionaries of the following form
                {
                    "name" (str): name of the parameter,
                    "type" (str): type of the parameter,
                    "value" (str): value of the parameter
                }

        Returns: A list of dictionaries that can be passed to the API call
    """

    if not parameters:
        return None

    bq_params = []
    for param in parameters:
        # construct parameter dict
        temp = {}
        temp["name"] = param["name"]
        temp["parameterType"] = {
            "type": param["type"],
        }
        temp["parameterValue"] = {
            "value": param["value"],
        }

        # append parameter dict to main list
        bq_params.append(temp)

    return bq_params


def wait_for_completion(projectId, jobId):
    """ Make requests periodically until the passed job has been completed """

    while True:

        try:
            job = bigquery.jobs().get(projectId=projectId, jobId=jobId).execute(num_retries=5)
        except googleapiclient.errors.HttpError as err:
            logger.error("HttpError while waiting for completion of job: {}".format(err))
            time.sleep(JOB_COMPLETION_CHECK_DELAY)
            continue

        if job["status"]["state"] == "DONE":
            return
        else:
            time.sleep(JOB_COMPLETION_CHECK_DELAY)

def format_results(data):
    """ The data returned by BigQuery contains a dict for the schema and a seperate dict for
        the rows. This function formats the data into a form similar to the data returned
        by sqlalchemy i.e. a dictionary keyed by row names
    """

    formatted_data = []
    for row in data['rows']:
        formatted_row = {}
        for index, val in enumerate(row['f']):
            formatted_row[data['schema']['fields'][index]['name']] = val['v']
        formatted_data.append(formatted_row)
    return formatted_data


def run_query(query, parameters=None):
    """ Run provided query on Google BigQuery and return the results in the form of a dictionary

        Note: This is a synchronous action
    """

    # Run the query
    query_body = {
        "kind": "bigquery#queryRequest",
        "parameterMode": "NAMED",
        "default_dataset": {
            "projectId": config.BIGQUERY_PROJECT_ID,
            "datasetId": config.BIGQUERY_DATASET_ID,
        },
        "useLegacySql": False,
        "queryParameters": get_parameters_dict(parameters),
        "query": query,
    }

    response = bigquery.jobs().query(
        projectId=config.BIGQUERY_PROJECT_ID,
        body=query_body).execute(num_retries=5)

    job_reference = response['jobReference']

    # Check response to see if query was completed before request timeout.
    # If it wasn't, wait until it has been completed.

    if not response['jobComplete']:
        wait_for_completion(**job_reference)
    else:
        have_results = True

    data = {}
    prev_token = None
    if have_results:
        first_page = response
    else:
        while True:
            try:
                first_page = bigquery.jobs().getQueryResults(**job_reference).execute(num_retries=5)
                break
            except googleapiclient.errors.HttpError as err:
                logger.error("HttpError when getting first page after completion of job: {}".format(err))
                time.sleep(JOB_COMPLETION_CHECK_DELAY)


    data['schema'] = first_page['schema']
    data['rows']   = first_page['rows']
    try:
        prev_token = first_page['pageToken']
    except KeyError:
        # if there is no page token, we have all the results
        # so just return the data
        return format_results(data)

    # keep making requests until we reach the last page and return the data
    # as soon as we do
    while True:
        try:
            query_result = bigquery.jobs().getQueryResults(pageToken=prev_token, **job_reference).execute(num_retries=5)
        except googleapiclient.errors.HttpError as err:
            logger.error("HttpError when getting query results: {}".format(err))
            continue

        data['rows'].extend(query_result['rows'])
        try:
            prev_token = query_result['pageToken']
        except KeyError:
            return format_results(data)
