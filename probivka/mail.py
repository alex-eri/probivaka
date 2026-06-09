import os
from typing import TypedDict
from imapclient import IMAPClient
import email
import logging
import io
import zipfile

class MailboxConfig(TypedDict):
    login:str
    password:str
    server:str


def fetchmail(mailbox :MailboxConfig):
    savepath = os.path.join(
        os.path.expanduser("~"), ".config", "Probivaka", mailbox['login']
    )
    os.makedirs(savepath, exist_ok=True)

    ret = []

    with IMAPClient(host=mailbox['server']) as client:
        r=client.login( mailbox['login'], mailbox['password'])
        logging.info(r)

        client.select_folder('INBOX')
        messages = client.search(["UNSEEN"])
        response = client.fetch(messages, ["RFC822"])

        for msg_id, message_data in response.items():
            raw_email = message_data[b'RFC822']
            email_message = email.message_from_bytes(raw_email)
            for part in email_message.walk():
            # Skip multi-part containers
                if part.get_content_maintype() == 'multipart':
                    continue
                
                # Check for attachments using Content-Disposition
                if part.get('Content-Disposition') is None:
                    continue

                # Extract filename and attachment data
                filename = part.get_filename()

                if filename:
                    file_data = part.get_payload(decode=True)
                    
                    if part.get_content_subtype() == 'plain':
                        name = os.path.join(savepath,filename)
                        with open(name, 'wb') as f:
                            f.write(file_data)
                            logging.info('Saved: %s', filename)
                            ret.append(name)
                    
                    if part.get_content_subtype() == 'zip':
                        # os.makedirs(os.path.join(savepath,filename),exist_ok=True)
                        bytes_stream = io.BytesIO(file_data)
                        with zipfile.ZipFile(bytes_stream, 'r') as archive:
                            for fn in archive.namelist():
                                file_content = archive.read(fn)
                                name = os.path.join(savepath,fn)
                                with open(name, 'wb') as f:
                                    f.write(file_content)
                                    logging.info('Saved: %s/%s', name )
                                    ret.append(name)
    return ret