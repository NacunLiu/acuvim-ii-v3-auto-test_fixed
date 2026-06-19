# Description: see below
# last updated by Nacun Liu 2024-06-06

"""
This feature provides option for hardworking test engineer to receive test result through email/text message 
while busy dedicating into other tasks.

To enable such feature, you need to create an aws account, then go to Amazon SNS to create a new topics,
then add your work email as a subscriber.

aws SNS is included in free-tier so you dont need to worry about the cost.

Once you created your topics and completed the subscription, you shall put your rootkey.csv to the same file
but DO PROTECT IT

Returns:
    None
"""
import boto3
import pandas as pd
import os
from botocore.exceptions import ClientError
import logging,coloredlogs

######################################################
logger = logging.getLogger(__name__)
coloredlogs.install(level='DEBUG', logger=logger, fmt='%(asctime)s %(hostname)s %(levelname)s %(message)s')
current_folder = os.getcwd()
csv_path = current_folder+'\\rootkey.csv'
try:
    df = pd.read_csv(csv_path)
    AK = str(df['Access key ID'][0])
    SK = str(df['Secret access key'][0])
    email = str(df['Email'])
    region = 'ca-central-1'  # central Canada Server
except FileNotFoundError as e:
    logger.info("Unable to locate rootkey.csv, aws sns service disabled")

class SnsWrapper:
    def __init__(self,snsResource): 
        self.sns_resource = snsResource
    
    def push_message(self,message):
        try:
            targetSns = "arn:aws:sns:ca-central-1:644115212646:AcuTextPush"
            response = self.sns_resource.publish(TopicArn = targetSns, Message = message, MessageAttributes={'email': {'DataType': 'String', 'StringValue': df['Email'][0]}})
            message_id = response['MessageId']

            logger.info("Send message to {}".format(df['Email'][0]))
        except ClientError:
            logger.exception("Send message Failure")
        else:
            return message_id
         
def run(message):
    global email
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        if AK is not None:
            sns_wrapper = SnsWrapper(boto3.client('sns',aws_access_key_id=AK,aws_secret_access_key = SK, region_name = region))
            sns_wrapper.push_message(message)
    except NameError:
        logger.warning('aws SNS disabled...')
        
if __name__ == '__main__':
    run('TEST DURATION TIME MESSAGE CONTEXT')