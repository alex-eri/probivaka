from io import TextIOWrapper
from .libfptr10 import IFptr
import re
import sqlite3
import os
from typing import ContextManager
from typing import Generator
from contextlib import contextmanager
import tkinter as tk
from tkinter import filedialog
from typing import TypedDict
import json
import datetime
from urllib.parse import urlencode, parse_qsl
import logging
import time
import codecs
import argparse
from . import mail


class Client(TypedDict):
    name: str
    inn: str


def wait_ofd(fptr: IFptr):
    for i in range(10):
        fptr.setParam(
            IFptr.LIBFPTR_PARAM_FN_DATA_TYPE, IFptr.LIBFPTR_FNDT_OFD_EXCHANGE_STATUS
        )
        fptr.fnQueryData()
        unsentCount = fptr.getParamInt(IFptr.LIBFPTR_PARAM_DOCUMENTS_COUNT)
        unsentFirstNumber = fptr.getParamInt(IFptr.LIBFPTR_PARAM_DOCUMENT_NUMBER)
        unsentDateTime: datetime.datetime = fptr.getParamDateTime(
            IFptr.LIBFPTR_PARAM_DATE_TIME
        )
        if unsentCount == 0:
            break
        logging.error(
            "Не переданно документов %d с %s, ждём",
            unsentCount,
            unsentDateTime.isoformat(),
        )
        time.sleep(60)


@contextmanager
def connection(settings):
    fptr = IFptr(lib_path="", fptr_id=settings["fptr_id"])

    fptr.data = dict()
    fptr.setSingleSetting(IFptr.LIBFPTR_SETTING_PORT, str(IFptr.LIBFPTR_PORT_USB))
    if settings["remote_server_addr"]:
        fptr.setSingleSetting(
            IFptr.LIBFPTR_SETTING_REMOTE_SERVER_ADDR, settings["remote_server_addr"]
        )
    fptr.applySingleSettings()

    fptr.open()

    if not fptr.isOpened():
        return

    # Регистрация кассира
    fptr.setParam(1021, settings["casher"]["name"])
    fptr.setParam(1203, settings["casher"]["inn"])
    fptr.operatorLogin()

    fptr.data["email"] = settings["email"]

    try:
        yield fptr
    finally:
        if fptr.isOpened():
            wait_ofd(fptr)
    fptr.close()


@contextmanager
def shift(fptr: IFptr):

    fptr.setParam(IFptr.LIBFPTR_PARAM_FN_DATA_TYPE, IFptr.LIBFPTR_FNDT_FN_INFO)
    fptr.fnQueryData()

    fnSerial = fptr.getParamString(IFptr.LIBFPTR_PARAM_SERIAL_NUMBER)

    fptr.data["fnSerial"] = fnSerial

    fptr.setParam(IFptr.LIBFPTR_PARAM_DATA_TYPE, IFptr.LIBFPTR_DT_STATUS)
    fptr.queryData()

    shiftState = fptr.getParamInt(IFptr.LIBFPTR_PARAM_SHIFT_STATE)

    if shiftState == IFptr.LIBFPTR_SS_OPENED:
        try:
            yield fptr
        except Exception as e:
            raise e
        return

    if shiftState == IFptr.LIBFPTR_SS_EXPIRED:
        fptr.setParam(IFptr.LIBFPTR_PARAM_REPORT_TYPE, IFptr.LIBFPTR_RT_CLOSE_SHIFT)
        fptr.report()
        fptr.checkDocumentClosed()

    fptr.openShift()
    fptr.checkDocumentClosed()
    try:
        yield fptr
    except Exception as e:
        raise e
    finally:
        fptr.setParam(IFptr.LIBFPTR_PARAM_REPORT_TYPE, IFptr.LIBFPTR_RT_CLOSE_SHIFT)
        fptr.report()

        fptr.checkDocumentClosed()


@contextmanager
def receipt(fptr: IFptr, client: Client = {}):
    clientInfo = None
    if client:
        fptr.setParam(1227, client["name"])
        fptr.setParam(1228, client["inn"])
        fptr.utilFormTlv()
        clientInfo = fptr.getParamByteArray(IFptr.LIBFPTR_PARAM_TAG_VALUE)

    if clientInfo:
        fptr.setParam(1256, clientInfo)

    if client.get("email") or client.get("phone"):
        fptr.setParam(1008, client.get("email") or client.get("phone"))
    else:
        fptr.setParam(1008, fptr.data["email"])

    fptr.setParam(IFptr.LIBFPTR_PARAM_RECEIPT_TYPE, IFptr.LIBFPTR_RT_SELL)
    fptr.setParam(IFptr.LIBFPTR_PARAM_RECEIPT_ELECTRONICALLY, True)
    fptr.openReceipt()
    try:
        yield fptr

        fptr.setParam(IFptr.LIBFPTR_PARAM_PAYMENT_TYPE, IFptr.LIBFPTR_PT_CREDIT)
        fptr.closeReceipt()

        while fptr.checkDocumentClosed() < 0:
            # Не удалось проверить состояние документа. Вывести пользователю текст ошибки, попросить устранить неполадку и повторить запрос
            print(fptr.errorDescription())
            continue

    except Exception as e:
        if not fptr.getParamBool(IFptr.LIBFPTR_PARAM_DOCUMENT_CLOSED):
            # Документ не закрылся. Требуется его отменить (если это чек) и сформировать заново
            fptr.cancelReceipt()
            raise e


def good(fptr: IFptr, name: str, price: float, prepaid=0):

    fptr.setParam(IFptr.LIBFPTR_PARAM_COMMODITY_NAME, name)
    fptr.setParam(IFptr.LIBFPTR_PARAM_PRICE, price)
    fptr.setParam(IFptr.LIBFPTR_PARAM_QUANTITY, 1)
    fptr.setParam(IFptr.LIBFPTR_PARAM_TAX_TYPE, IFptr.LIBFPTR_TAX_NO)
    fptr.setParam(1212, 4)  # Услуга
    fptr.setParam(1214, 7)  # Оплата кредита

    fptr.registration()


def payment(fptr: IFptr, name: str, price: float, prepaid=0):
    fptr.setParam(IFptr.LIBFPTR_PARAM_PAYMENT_TYPE, IFptr.LIBFPTR_PT_ELECTRONICALLY)
    fptr.setParam(IFptr.LIBFPTR_PARAM_PAYMENT_SUM, price)
    fptr.payment()


def parse_kl_to_1cf(f: TextIOWrapper):

    def parse_doc(doc, f):
        for line in f:
            if "=" in line:
                k, v = line.split("=", maxsplit=1)
                doc[k.strip()] = v.strip()
                continue
            if line.strip():
                break

    meta = {"rs": []}
    docs = []
    line = next(f)
    assert line.strip() == "1CClientBankExchange"
    for line in f:
        if "=" in line:
            k, v = line.split("=", maxsplit=1)
            k = k.strip()
            v = v.strip()
            if k == "СекцияДокумент":
                doc = dict({k: v})
                parse_doc(doc, f)
                docs.append(doc)
            if k == "РасчСчет":
                rs = dict({k: v})
                parse_doc(rs, f)
                meta["rs"].append(rs)

    return meta, docs


@contextmanager
def db() -> Generator[sqlite3.Cursor, None, None]:
    appdata = os.path.join(os.path.expanduser("~"), ".config", "Probivaka")
    os.makedirs(appdata, exist_ok=True)
    con = sqlite3.connect(os.path.join(appdata, "cheks.db"))

    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS Чеки (
            id INTEGER PRIMARY KEY,
            ПолучательИНН TEXT, 
            Номер TEXT ,
            Дата TEXT ,
            Сумма TEXT ,
            ПлательщикИНН TEXT ,
            Плательщик1 TEXT ,
            НазначениеПлатежа TEXT ,
            ЛицевойСчёт TEXT,
            Чек TEXT
        )
    """)

    yield cur
    con.commit()
    con.close()


def get_check(fptr: IFptr):
    fptr.setParam(IFptr.LIBFPTR_PARAM_FN_DATA_TYPE, IFptr.LIBFPTR_FNDT_LAST_RECEIPT)
    fptr.fnQueryData()

    documentNumber = fptr.getParamInt(IFptr.LIBFPTR_PARAM_DOCUMENT_NUMBER)
    receiptSum = fptr.getParamDouble(IFptr.LIBFPTR_PARAM_RECEIPT_SUM)
    fiscalSign = fptr.getParamString(IFptr.LIBFPTR_PARAM_FISCAL_SIGN)
    dateTime: datetime.datetime = fptr.getParamDateTime(IFptr.LIBFPTR_PARAM_DATE_TIME)
    receiptType = fptr.getParamInt(IFptr.LIBFPTR_PARAM_RECEIPT_TYPE)

    return dict(
        t=dateTime.strftime("%Y%m%dT%H%M"),
        s=receiptSum,
        n=receiptType,
        i=documentNumber,
        fp=fiscalSign,
        fn=fptr.data.get("fnSerial"),
    )


def sell(shift, cursor: sqlite3.Cursor, doc):
    cursor.execute(
        """
        INSERT INTO Чеки (
            ПолучательИНН , 
            Номер  ,
            Дата  ,
            Сумма  ,
            ПлательщикИНН  ,
            Плательщик1  ,
            НазначениеПлатежа ,
            ЛицевойСчёт
        ) VALUES ( ?, ?, ?,?, ?, ?, ?,?) 
        RETURNING id
    """,
        (
            doc["ПолучательИНН"],
            doc["Номер"],
            doc["Дата"],
            doc["Сумма"],
            doc["ПлательщикИНН"],
            doc["Плательщик1"],
            doc["НазначениеПлатежа"],
            doc.get("ЛицевойСчёт"),
        ),
    )
    row = cursor.fetchone()
    inserted_id = row[0] if row else None

    cursor.connection.commit()

    with receipt(
        shift,
        client={
            "inn": doc["ПлательщикИНН"],
            "name": doc["Плательщик1"],
        },
    ) as r:
        n = doc["НазначениеПлатежа"]
        if ls := doc.get("ЛицевойСчёт"):
            n = f"Оплата по ЛС {ls}"

        s = float(doc["Сумма"])

        good(r, n, s, 0)
        payment(r, n, s, 0)

        r.setParam(IFptr.LIBFPTR_PARAM_SUM, s)
        r.receiptTotal()

    chek = get_check(shift)

    cursor.execute(
        """
        UPDATE Чеки SET Чек = ? WHERE id = ? 
        """,
        (urlencode(chek), inserted_id),
    )

    doc["Чек"] = chek

    cursor.connection.commit()


def get_kl_to_1c():
    root = tk.Tk()
    root.withdraw()
    file_paths = filedialog.askopenfilename(
        title="Выписки к пробивке в формате 1с",
        filetypes=(
            (
                "Выписка",
                "kl_to_1c_*.txt",
            ),
        ),
        multiple=True,
    )

    return file_paths


def check_already(cur: sqlite3.Cursor, doc):
    #         """
    #     INSERT INTO Чеки (
    #         ПолучательИНН ,
    #         Номер  ,
    #         Дата  ,
    #         Сумма  ,
    #         ПлательщикИНН  ,
    #         Плательщик1  ,
    #         НазначениеПлатежа ,
    #         ЛицевойСчёт
    #     ) VALUES ( ?, ?, ?,?, ?, ?, ?,?)
    #     RETURNING id
    # """
    cur.execute(
        """
        SELECT Чек FROM Чеки WHERE 
             ПолучательИНН = ? AND
             Номер = ? AND
             Дата   = ? AND
             Сумма  = ? AND
             ПлательщикИНН = ? AND
             Плательщик1 = ? AND
             НазначениеПлатежа = ?
        """,
        [
            doc["ПолучательИНН"],
            doc["Номер"],
            doc["Дата"],
            doc["Сумма"],
            doc["ПлательщикИНН"],
            doc["Плательщик1"],
            doc["НазначениеПлатежа"],
        ],
    )

    res = cur.fetchone()
    doc["Чек"] = dict(parse_qsl(res[0])) if res and res[0] else None
    return doc["Чек"]


def detect_cp(file_path):
    cp = None
    with open(file_path, "rb") as fb:
        for line in fb:
            if b"=" not in line:
                continue
            k, v = line.split(b"=")
            if v.strip().upper() == b"Windows".upper():
                cp = "cp1251"
                break
            if v.strip().upper() == b"DOS".upper():
                cp = "cp866"
                break
            if v.strip().upper() == b"UTF-8".upper():
                cp = "utf_8"
                break
    if not cp:
        raise Exception("BadFormat: Codepage")
    return cp


def loadcsv(file_path, settings):
    lsmatch = re.compile(r"(\D|^)(2\d{8})(\D|$)")

    logging.warning("=== Загрузка %s ===", file_path)

    meta = {}
    docs = []

    cp = detect_cp(file_path)

    with codecs.open(file_path, "r", cp) as f:
        meta, docs = parse_kl_to_1cf(f)

    recipients = set()

    for doc in docs:
        # if len(doc["ПлательщикИНН"]) != 12:
        #     continue

        np = doc["НазначениеПлатежа"]
        lssearch = lsmatch.search(np)

        if lssearch:
            doc["ЛицевойСчёт"] = lssearch.groups()[1]
            # print(doc["ЛицевойСчёт"])
        # else:
        #     print(
        #         doc["НазначениеПлатежа"],
        #         sep="\t",
        #     )

        recipients.add(doc["ПолучательИНН"])

    logging.warning("=== Пробивка ===")

    for rct in recipients:
        rctdocs = list(
            filter(
                lambda x: (
                    x["ПолучательИНН"] == rct
                    and x["ПолучательИНН"] != x["ПлательщикИНН"]
                    and (
                        x["ПлательщикИНН"] == ""
                        or x["ПлательщикИНН"] == "0"
                        or len(x["ПлательщикИНН"]) == 12
                    )
                ),
                docs,
            )
        )

        if setting := settings.get(rct):
            logging.warning("Документов: %s", len(rctdocs))

            with db() as cur:

                for doc in rctdocs:
                    print(
                        doc["ДатаПоступило"],
                        f'\033[31m{doc["Сумма"]}\033[0m',
                        doc["Плательщик1"],
                        f'\033[32m{doc.get("ЛицевойСчёт", "!!!")}\033[0m',
                        doc["НазначениеПлатежа"],
                        sep="\t",
                        end="\t",
                    )
                    if check_already(cur, doc):
                        print("✅", doc["Чек"]["i"])
                    else:
                        print("☐")

            rctdocs = list(
                filter(
                    lambda x: not x.get("Чек"),
                    rctdocs,
                )
            )

            logging.warning("Документов не пробито: %s", len(rctdocs))

            for doc in rctdocs:
                print(
                    doc["ДатаПоступило"],
                    f'\033[31m{doc["Сумма"]}\033[0m',
                    doc["Плательщик1"],
                    f'\033[32m{doc.get("ЛицевойСчёт", "!!!")}\033[0m',
                    doc["НазначениеПлатежа"],
                    sep="\t",
                )

            yesno = input("\n\nПробиваем?")
            if yesno.lower().strip() not in ["да", "д", "y", "yes"]:
                continue

            logging.info("Подключаю кассу")
            with connection(setting) as con, db() as cur:
                logging.info("Открываю смену")
                with shift(con) as s:
                    logging.info("Пробиваю приход")
                    for doc in rctdocs:
                        print(
                            doc["ДатаПоступило"],
                            f'\033[31m{doc["Сумма"]}\033[0m',
                            (
                                f'\033[32m{doc.get("ЛицевойСчёт")}\033[0m'
                                if doc.get("ЛицевойСчёт")
                                else doc["НазначениеПлатежа"]
                            ),
                            sep="\t",
                            end="\t",
                        )
                        if doc.get("Чек"):
                            print("🔢", doc["Чек"]["i"])
                        else:
                            sell(s, cur, doc)
                            print("✅", doc["Чек"]["i"])

    logging.warning("=== Конец пробивки ===")


def main():
    settings_path = os.path.join(
        os.path.expanduser("~"), ".config", "Probivaka", "settings.json"
    )
    try:
        with open(settings_path, "r") as fp:
            settings = json.load(fp)
    except Exception as e:
        logging.critical("Создайте файл настроек '%s' !", settings_path)
        raise e

    parser = argparse.ArgumentParser(description="Пробивка банковских выписок")
    parser.add_argument("-a", "--askopen", action="store_true")
    parser.add_argument("-e", "--email", action="store_true")

    args = parser.parse_args()

    file_paths = []

    if args.askopen:
        file_paths += get_kl_to_1c()

    if args.email:
        for mb in settings.get("mailboxes", []):
            file_paths += mail.fetchmail(mb)

    done = []
    undone = []

    for file_path in file_paths:
        try:
            loadcsv(file_path, settings)
        except Exception as e:
            logging.error(e)
            os.rename(file_path, file_path + ".error")
            undone.append("error-" + file_path)
        else:
            os.rename(file_path, file_path + ".done")
            done.append("done-" + file_path)

    if undone:
        logging.warning("Файлы с ошибками: %s", repr(undone))


if __name__ == "__main__":
    main()
