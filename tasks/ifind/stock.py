# -*- coding: utf-8 -*-
"""
Created on 2018/1/17
@author: MG
"""

import logging
import math
from datetime import date, datetime, timedelta
import pandas as pd
from sqlalchemy.exc import ProgrammingError
from tasks.ifind import invoker
from direstinvoker.ifind import APIError
from tasks.utils.fh_utils import get_last, get_first, date_2_str, STR_FORMAT_DATE, str_2_date
from sqlalchemy.types import String, Date, Integer
from sqlalchemy.dialects.mysql import DOUBLE
from tasks.utils.fh_utils import unzip_join
from tasks.utils.db_utils import with_db_session, add_col_2_table
from tasks.backend import engine_md
from tasks import app
DEBUG = False
logger = logging.getLogger()
DATE_BASE = datetime.strptime('1990-01-01', STR_FORMAT_DATE).date()
ONE_DAY = timedelta(days=1)
# 标示每天几点以后下载当日行情数据
BASE_LINE_HOUR = 20


def get_stock_code_set(date_fetch):
    date_fetch_str = date_fetch.strftime(STR_FORMAT_DATE)
    stock_df = invoker.THS_DataPool('block', date_fetch_str + ';001005010', 'thscode:Y,security_name:Y')
    if stock_df is None:
        logging.warning('%s 获取股票代码失败', date_fetch_str)
        return None
    stock_count = stock_df.shape[0]
    logging.info('get %d stocks on %s', stock_count, date_fetch_str)
    return set(stock_df['THSCODE'])


@app.task
def import_stock_info(ths_code=None, refresh=False):
    """

    :param ths_code:
    :param refresh:
    :return:
    """
    logging.info("更新 wind_stock_info 开始")
    if ths_code is None:
        # 获取全市场股票代码及名称
        if refresh:
            date_fetch = datetime.strptime('1991-02-01', STR_FORMAT_DATE).date()
        else:
            date_fetch = date.today()

        date_end = date.today()
        stock_code_set = set()
        while date_fetch < date_end:
            stock_code_set_sub = get_stock_code_set(date_fetch)
            if stock_code_set_sub is not None:
                stock_code_set |= stock_code_set_sub
            date_fetch += timedelta(days=365)

        stock_code_set_sub = get_stock_code_set(date_end)
        if stock_code_set_sub is not None:
            stock_code_set |= stock_code_set_sub

        ths_code = ','.join(stock_code_set)

    indicator_param_list = [
        ('ths_stock_short_name_stock', '', String(10)),
        ('ths_stock_code_stock', '', String(10)),
        ('ths_stock_varieties_stock', '', String(10)),
        ('ths_ipo_date_stock', '', Date),
        ('ths_listing_exchange_stock', '', String(10)),
        ('ths_delist_date_stock', '', Date),
        ('ths_corp_cn_name_stock', '', String(40)),
        ('ths_corp_name_en_stock', '', String(100)),
        ('ths_established_date_stock', '', Date),
    ]
    # jsonIndicator='ths_stock_short_name_stock;ths_stock_code_stock;ths_thscode_stock;ths_stock_varieties_stock;ths_ipo_date_stock;ths_listing_exchange_stock;ths_delist_date_stock;ths_corp_cn_name_stock;ths_corp_name_en_stock;ths_established_date_stock'
    # jsonparam=';;;;;;;;;'
    indicator, param = unzip_join([(key, val) for key, val, _ in indicator_param_list], sep=';')
    data_df = invoker.THS_BasicData(ths_code, indicator, param)
    if data_df is None or data_df.shape[0] == 0:
        logging.info("没有可用的 stock info 可以更新")
        return
    # 删除历史数据，更新数据
    with with_db_session(engine_md) as session:
        session.execute(
            "DELETE FROM ifind_stock_info WHERE ths_code IN (" + ','.join(
                [':code%d' % n for n in range(len(stock_code_set))]
            ) + ")",
            params={'code%d' % n: val for n, val in enumerate(stock_code_set)})
        session.commit()
    dtype = {key: val for key, _, val in indicator_param_list}
    dtype['ths_code'] = String(20)
    data_count = data_df.shape[0]
    data_df.to_sql('ifind_stock_info', engine_md, if_exists='append', index=False, dtype=dtype)
    logging.info("更新 ifind_stock_info 完成 存量数据 %d 条", data_count)


@app.task
def import_stock_daily_ds(ths_code_set: set = None, begin_time=None):
    """
    通过date_serise接口将历史数据保存到 ifind_stock_daily_ds，该数据作为 History数据的补充数据 例如：复权因子af、涨跌停标识、停牌状态、原因等
    :param ths_code_set:
    :param begin_time:
    :return:
    """
    indicator_param_list = [
        ('ths_af_stock', '', DOUBLE),
        ('ths_up_and_down_status_stock', '', String(10)),
        ('ths_trading_status_stock', '', String(80)),
        ('ths_suspen_reason_stock', '', String(80)),
        ('ths_last_td_date_stock', '', Date),
    ]
    # jsonIndicator='ths_pre_close_stock;ths_open_price_stock;ths_high_price_stock;ths_low_stock;ths_close_price_stock;ths_chg_ratio_stock;ths_chg_stock;ths_vol_stock;ths_trans_num_stock;ths_amt_stock;ths_turnover_ratio_stock;ths_vaild_turnover_stock;ths_af_stock;ths_up_and_down_status_stock;ths_trading_status_stock;ths_suspen_reason_stock;ths_last_td_date_stock'
    # jsonparam='100;100;100;100;100;;100;100;;;;;;;;;'
    json_indicator, json_param = unzip_join([(key, val) for key, val, _ in indicator_param_list], sep=';')
    if engine_md.has_table('ifind_stock_daily_ds'):
        sql_str = """SELECT ths_code, date_frm, if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) date_to
            FROM
            (
                SELECT info.ths_code, ifnull(trade_date_max_1, ths_ipo_date_stock) date_frm, ths_delist_date_stock,
                if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                FROM 
                    ifind_stock_info info 
                LEFT OUTER JOIN
                    (SELECT ths_code, adddate(max(time),1) trade_date_max_1 FROM ifind_stock_daily_ds GROUP BY ths_code) daily
                ON info.ths_code = daily.ths_code
            ) tt
            WHERE date_frm <= if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) 
            ORDER BY ths_code"""
    else:
        sql_str = """SELECT ths_code, date_frm, if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) date_to
            FROM
            (
                SELECT info.ths_code, ths_ipo_date_stock date_frm, ths_delist_date_stock,
                if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                FROM ifind_stock_info info 
            ) tt
            WHERE date_frm <= if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) 
            ORDER BY ths_code"""
        logger.warning('ifind_stock_daily_ds 不存在，仅使用 ifind_stock_info 表进行计算日期范围')
    with with_db_session(engine_md) as session:
        # 获取每只股票需要获取日线数据的日期区间
        table = session.execute(sql_str)

        table = session.execute(sql_str)

        # 获取每只股票需要获取日线数据的日期区间
        code_date_range_dic = {
            ths_code: (date_from if begin_time is None else min([date_from, begin_time]), date_to)
            for ths_code, date_from, date_to in table.fetchall() if
            ths_code_set is None or ths_code in ths_code_set}
    # 设置 dtype
    dtype = {key: val for key, _, val in indicator_param_list}
    dtype['ths_code'] = String(20)
    dtype['time'] = Date

    data_df_list, data_count, tot_data_count, code_count = [], 0, 0, len(code_date_range_dic)
    try:
        for num, (ths_code, (begin_time, end_time)) in enumerate(code_date_range_dic.items(), start=1):
            logger.debug('%d/%d) %s [%s - %s]', num, code_count, ths_code, begin_time, end_time)
            data_df = invoker.THS_DateSerial(
                ths_code,
                json_indicator,
                json_param,
                'Days:Tradedays,Fill:Previous,Interval:D',
                begin_time, end_time
            )
            if data_df is not None or data_df.shape[0] > 0:
                data_count += data_df.shape[0]
                data_df_list.append(data_df)
            # 大于阀值有开始插入
            if data_count >= 10000:
                tot_data_df = pd.concat(data_df_list)
                tot_data_df.to_sql('ifind_stock_daily_ds', engine_md, if_exists='append', index=False, dtype=dtype)
                tot_data_count += data_count
                data_df_list, data_count = [], 0

            # 仅调试使用
            if DEBUG and len(data_df_list) > 1:
                break
    finally:
        if data_count > 0:
            tot_data_df = pd.concat(data_df_list)
            tot_data_df.to_sql('ifind_stock_daily_ds', engine_md, if_exists='append', index=False, dtype=dtype)
            tot_data_count += data_count

        logging.info("更新 ifind_stock_daily 完成 新增数据 %d 条", tot_data_count)


@app.task
def import_stock_daily_his(ths_code_set: set = None, begin_time=None):
    """
    通过history接口将历史数据保存到 ifind_stock_daily_his
    :param ths_code_set:
    :param begin_time: 默认为None，如果非None则代表所有数据更新日期不得晚于该日期
    :return:
    """
    if begin_time is not None and type(begin_time) == date:
        begin_time = str_2_date(begin_time)

    indicator_param_list = [
        ('preClose', '', DOUBLE),
        ('open', '', DOUBLE),
        ('high', '', DOUBLE),
        ('low', '', DOUBLE),
        ('close', '', DOUBLE),
        ('avgPrice', '', DOUBLE),
        ('changeRatio', '', DOUBLE),
        ('volume', '', DOUBLE),
        ('amount', '', DOUBLE),
        ('turnoverRatio', '', DOUBLE),
        ('transactionAmount', '', DOUBLE),
        ('totalShares', '', DOUBLE),
        ('totalCapital', '', DOUBLE),
        ('floatSharesOfAShares', '', DOUBLE),
        ('floatSharesOfBShares', '', DOUBLE),
        ('floatCapitalOfAShares', '', DOUBLE),
        ('floatCapitalOfBShares', '', DOUBLE),
        ('pe_ttm', '', DOUBLE),
        ('pe', '', DOUBLE),
        ('pb', '', DOUBLE),
        ('ps', '', DOUBLE),
        ('pcf', '', DOUBLE),
    ]
    # THS_HistoryQuotes('600006.SH,600010.SH',
    # 'preClose,open,high,low,close,avgPrice,changeRatio,volume,amount,turnoverRatio,transactionAmount,totalShares,totalCapital,floatSharesOfAShares,floatSharesOfBShares,floatCapitalOfAShares,floatCapitalOfBShares,pe_ttm,pe,pb,ps,pcf',
    # 'Interval:D,CPS:1,baseDate:1900-01-01,Currency:YSHB,fill:Previous',
    # '2018-06-30','2018-07-30')
    json_indicator, _ = unzip_join([(key, val) for key, val, _ in indicator_param_list], sep=';')
    if engine_md.has_table('ifind_stock_daily_his'):
        sql_str = """SELECT ths_code, date_frm, if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) date_to
            FROM
            (
                SELECT info.ths_code, ifnull(trade_date_max_1, ths_ipo_date_stock) date_frm, ths_delist_date_stock,
                if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                FROM 
                    ifind_stock_info info 
                LEFT OUTER JOIN
                    (SELECT ths_code, adddate(max(time),1) trade_date_max_1 FROM ifind_stock_daily_his GROUP BY ths_code) daily
                ON info.ths_code = daily.ths_code
            ) tt
            WHERE date_frm <= if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) 
            ORDER BY ths_code"""
    else:
        logger.warning('ifind_stock_daily_his 不存在，仅使用 ifind_stock_info 表进行计算日期范围')
        sql_str = """SELECT ths_code, date_frm, if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) date_to
            FROM
            (
                SELECT info.ths_code, ths_ipo_date_stock date_frm, ths_delist_date_stock,
                if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                FROM ifind_stock_info info 
            ) tt
            WHERE date_frm <= if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) 
            ORDER BY ths_code"""

    # 计算每只股票需要获取日线数据的日期区间
    with with_db_session(engine_md) as session:
        # 获取每只股票需要获取日线数据的日期区间
        table = session.execute(sql_str)
        # 计算每只股票需要获取日线数据的日期区间
        code_date_range_dic = {
            ths_code: (date_from if begin_time is None else min([date_from, begin_time]), date_to)
            for ths_code, date_from, date_to in table.fetchall() if
            ths_code_set is None or ths_code in ths_code_set}
    # 设置 dtype
    dtype = {key: val for key, _, val in indicator_param_list}
    dtype['ths_code'] = String(20)
    dtype['time'] = Date

    data_df_list, data_count, tot_data_count, code_count = [], 0, 0, len(code_date_range_dic)
    try:
        for num, (ths_code, (begin_time, end_time)) in enumerate(code_date_range_dic.items(), start=1):
            logger.debug('%d/%d) %s [%s - %s]', num, code_count, ths_code, begin_time, end_time)
            data_df = invoker.THS_HistoryQuotes(
                ths_code,
                json_indicator,
                'Interval:D,CPS:1,baseDate:1900-01-01,Currency:YSHB,fill:Previous',
                begin_time, end_time
            )
            if data_df is not None or data_df.shape[0] > 0:
                data_count += data_df.shape[0]
                data_df_list.append(data_df)
            # 大于阀值有开始插入
            if data_count >= 10000:
                data_count = save_ifind_stock_daily_his(data_df_list, dtype)
                tot_data_count += data_count
                data_df_list, data_count = [], 0
    finally:
        if data_count > 0:
            data_count = save_ifind_stock_daily_his(data_df_list, dtype)
            tot_data_count += data_count

        logging.info("更新 ifind_stock_daily_his 完成 新增数据 %d 条", tot_data_count)


def save_ifind_stock_daily_his(data_df_list, dtype):
    """保存数据到 ifind_stock_daily_his"""
    if len(data_df_list) > 0:
        tot_data_df = pd.concat(data_df_list)
        # TODO: 需要解决重复数据插入问题，日后改为sql语句插入模式
        tot_data_df.to_sql('ifind_stock_daily_his', engine_md, if_exists='append', index=False, dtype=dtype)
        data_count = tot_data_df.shape[0]
        logger.info('保存数据到 ifind_stock_daily_his 成功，包含 %d 条记录', data_count)
        return data_count
    else:
        return 0


@app.task
def add_new_col_data(col_name, param, db_col_name=None, col_type_str='DOUBLE', ths_code_set: set = None):
    """
    1）修改 daily 表，增加字段
    2）ckpv表增加数据
    3）第二部不见得1天能够完成，当第二部完成后，将ckvp数据更新daily表中
    :param col_name:增加字段名称
    :param param: 参数
    :param dtype: 数据库字段类型
    :param db_col_name: 默认为 None，此时与col_name相同
    :param col_type_str: DOUBLE, VARCHAR(20), INTEGER, etc. 不区分大小写
    :param ths_code_set: 默认 None， 否则仅更新指定 ths_code
    :return:
    """
    if db_col_name is None:
        # 默认为 None，此时与col_name相同
        db_col_name = col_name

    # 检查当前数据库是否存在 db_col_name 列，如果不存在则添加该列
    add_col_2_table(engine_md, 'ifind_stock_daily_ds', db_col_name, col_type_str)
    # 将数据增量保存到 ckdvp 表
    all_finished = add_data_2_ckdvp(col_name, param, ths_code_set)
    # 将数据更新到 ds 表中
    if all_finished:
        sql_str = """update ifind_stock_daily_ds daily, ifind_ckdvp_stock ckdvp
        set daily.{db_col_name} = ckdvp.value
        where daily.ths_code = ckdvp.ths_code
        and daily.time = ckdvp.time
        and ckdvp.key = '{db_col_name}'
        and ckdvp.param = '{param}'""".format(db_col_name=db_col_name, param=param)
        with with_db_session(engine_md) as session:
            session.execute(sql_str)
            session.commit()
        logger.info('更新 %s 字段 ifind_stock_daily_ds 表', db_col_name)


def add_data_2_ckdvp(json_indicator, json_param, ths_code_set: set = None, begin_time=None):
    """
    将数据增量保存到 ifind_ckdvp_stock 表，code key date value param 五个字段组合成的表 value 为 Varchar(80)
    该表用于存放各种新增加字段的值
    查询语句举例：
    THS_DateSerial('600007.SH,600009.SH','ths_pe_ttm_stock','101','Days:Tradedays,Fill:Previous,Interval:D','2018-07-31','2018-07-31')
    :param json_indicator:
    :param json_param:
    :param ths_code_set:
    :param begin_time:
    :return: 全部数据加载完成，返回True，否则False，例如数据加载中途流量不够而中断
    """
    all_finished = False
    has_table = engine_md.has_table('ifind_ckdvp_stock')
    if has_table:
        sql_str = """
            select ths_code, date_frm, if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) date_to
            FROM
            (
                select info.ths_code, ifnull(trade_date_max_1, ths_ipo_date_stock) date_frm, ths_delist_date_stock,
                    if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                from 
                    ifind_stock_info info 
                left outer join
                    (select ths_code, adddate(max(time),1) trade_date_max_1 from ifind_ckdvp_stock 
                        where ifind_ckdvp_stock.key='{0}' and param='{1}' group by ths_code
                    ) daily
                on info.ths_code = daily.ths_code
            ) tt
            where date_frm <= if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) 
            order by ths_code""".format(json_indicator, json_param)
    else:
        logger.warning('ifind_ckdvp_stock 不存在，仅使用 ifind_stock_info 表进行计算日期范围')
        sql_str = """
            SELECT ths_code, date_frm, 
                if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) date_to
            FROM
            (
                SELECT info.ths_code, ths_ipo_date_stock date_frm, ths_delist_date_stock,
                if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                FROM ifind_stock_info info 
            ) tt
            WHERE date_frm <= if(ths_delist_date_stock<end_date, ths_delist_date_stock, end_date) 
            ORDER BY ths_code"""

    # 计算每只股票需要获取日线数据的日期区间
    with with_db_session(engine_md) as session:
        # 获取每只股票需要获取日线数据的日期区间
        table = session.execute(sql_str)
        # 计算每只股票需要获取日线数据的日期区间
        code_date_range_dic = {
            ths_code: (date_from if begin_time is None else min([date_from, begin_time]), date_to)
            for ths_code, date_from, date_to in table.fetchall() if
            ths_code_set is None or ths_code in ths_code_set}

    # 设置 dtype
    dtype = {
        'ths_code': String(20),
        'key': String(80),
        'time': Date,
        'value': String(80),
        'param': String(80),
    }
    data_df_list, data_count, tot_data_count, code_count = [], 0, 0, len(code_date_range_dic)
    try:
        for num, (ths_code, (begin_time, end_time)) in enumerate(code_date_range_dic.items(), start=1):
            logger.debug('%d/%d) %s [%s - %s]', num, code_count, ths_code, begin_time, end_time)
            data_df = invoker.THS_DateSerial(
                ths_code,
                json_indicator,
                json_param,
                'Days:Tradedays,Fill:Previous,Interval:D',
                begin_time, end_time
            )
            if data_df is not None or data_df.shape[0] > 0:
                data_df['key'] = json_indicator
                data_df['param'] = json_param
                data_df.rename(columns={json_indicator: 'value'}, inplace=True)
                data_count += data_df.shape[0]
                data_df_list.append(data_df)

            # 大于阀值有开始插入
            if data_count >= 10000:
                tot_data_df = pd.concat(data_df_list)
                tot_data_df.to_sql('ifind_ckdvp_stock', engine_md, if_exists='append', index=False, dtype=dtype)
                tot_data_count += data_count
                data_df_list, data_count = [], 0

            # 仅调试使用
            if DEBUG and len(data_df_list) > 1:
                break

            all_finished = True
    finally:
        if data_count > 0:
            tot_data_df = pd.concat(data_df_list)
            tot_data_df.to_sql('ifind_ckdvp_stock', engine_md, if_exists='append', index=False, dtype=dtype)
            tot_data_count += data_count

        if not has_table:
            create_pk_str = """ALTER TABLE ifind_ckdvp_stock
                CHANGE COLUMN `ths_code` `ths_code` VARCHAR(20) NOT NULL ,
                CHANGE COLUMN `time` `time` DATE NOT NULL ,
                CHANGE COLUMN `key` `key` VARCHAR(80) NOT NULL ,
                CHANGE COLUMN `param` `param` VARCHAR(80) NOT NULL ,
                ADD PRIMARY KEY (`ths_code`, `time`, `key`, `param`)"""
            with with_db_session(engine_md) as session:
                session.execute(create_pk_str)

        logging.info("更新 ifind_ckdvp_stock 完成 新增数据 %d 条", tot_data_count)

    return all_finished


if __name__ == "__main__":
    # DEBUG = True
    # ths_code = None  # '600006.SH,600009.SH'
    # 股票基本信息数据加载
    # import_stock_info(ths_code)
    # 股票日K数据加载
    ths_code_set = None  # {'600006.SH', '600009.SH'}
    import_stock_daily_ds(ths_code_set)
    # 股票日K历史数据加载
    import_stock_daily_his(ths_code_set)
    # 添加新字段
    # add_new_col_data('ths_pe_ttm_stock', '101', ths_code_set=ths_code_set)
