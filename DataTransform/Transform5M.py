from datetime import datetime, timedelta, time
from datetime import time
from sqlalchemy.orm import sessionmaker
from time import sleep
import Common.config as config
import numpy as np
import pandas as pd
import sys, traceback
import multiprocessing as mp

from FeatureExtractor import PriceAmplitude, PriceVec, PriceChange, \
    CCI, PriceMA, VolMA, Turnover, RSI, KDJ, BIAS, BOLL, ROC, \
    VR, WR, MI, OSCV, DMA, EMV, EXPMA, ARBR, DMI, ASI, MACD, PSY, WVAD

TABLE_NAME_DAILY = "raw_stock_trading_daily"
TABLE_NAME_5MIN = "raw_stock_trading_5min"
TABLE_NAME_5MIN_EXTRACTED = "feature_extracted_stock_trading_5min"
TABLE_NAME_5MIN_SCALED = "feature_scaled_stock_trading_5min"
TABLE_NAME_5MIN_RESULT = "result_stock_trading_5min"

SAMPLE_DATE_OFFSET = 30  # 提取多少天范围内的缩放数据样本 用于获取最大 最小价格
SAMPLE_DATE_BIAS = 7  # 提前多少天来选取缩放数据样本

DATE_OFFSET = 2  # 预处理多少天的数据


class Transform5M:
    db = None
    code = None
    date = datetime.now().date()

    def __init__(self, code, date):
        self.code = code
        self.date = date
        self._limit_sample_start = None
        self._limit_sample_end = None
        self._shifted_date = None
        self._next_trading_date = None
        self._vol_min = None
        self._vol_avg = None
        self._vol_max = None
        self._price_min = None
        self._price_avg = None
        self._price_max = None
        self._count_min = None
        self._count_max = None
        self._data = None
        self._daily_df = None
        self._feature_extracted_data = None
        self._feature_scaled_data = None
        self._result_data = None
        return

    def _get_shifted_startdate(self):
        if self._shifted_date is not None:
            return self._shifted_date

        sql = "SELECT `date` " \
              "FROM {0} " \
              "WHERE `code`='{1}' AND `date`>='{2}' " \
              "ORDER BY `date` ASC " \
              "LIMIT 0,1".format(
            TABLE_NAME_DAILY, self.code, self.date
        )
        rs = self.db.execute(sql)
        data = rs.fetchone()
        if data is None:
            raise RuntimeError('No more trading date for {0} at or after {1}'.format(self.code, self.date))
            return None

        rs = self.db.execute(
            "SELECT `date` "
            "FROM {0} "
            "WHERE `code`='{1}' AND `date`<'{2}' "
            "ORDER BY `date` DESC "
            "LIMIT {3},1".format(
                TABLE_NAME_DAILY, self.code, self.date, (DATE_OFFSET - 1)
            )
        )
        data = rs.fetchone()
        if data is not None:
            shifted_date = data[0]
        else:
            raise RuntimeError('Ignore the date, because there are '
                               'no more data before that date'.format(self.code, self.date))
            return None

        next_trading_date = self._get_next_trading_date()
        if next_trading_date is None:
            return None

        # 测试两个日期差 如果跨度大于一周，那么就返回错误（最长的假期也就是黄金周）
        date_diff = next_trading_date - shifted_date
        if date_diff.days > 7:
            raise RuntimeError('Ignore the date, because there are '
                               'too much date distance since last trading day'.format(self.code, self.date))
            return None
        else:
            self._shifted_date = shifted_date
            return shifted_date

    def _get_next_trading_date(self):
        if self._next_trading_date is not None:
            return self._next_trading_date

        sql = "SELECT `date` " \
              "FROM {0} " \
              "WHERE `code`='{1}' AND `date`>'{2}' " \
              "ORDER BY `date` ASC " \
              "LIMIT 0,1".format(
            TABLE_NAME_DAILY, self.code, self.date
        )
        rs = self.db.execute(sql)
        data = rs.fetchone()
        if data is None:
            raise RuntimeError('No more trading date for {0} at or after {1}'.format(self.code, self.date))
            return None

        next_trading_date = data[0]

        # 测试两个日期差 如果跨度大于一周，那么就返回错误（最长的假期也就是黄金周）
        date_diff = next_trading_date - self.date
        if date_diff.days > 7:
            raise RuntimeError('Ignore the date, because there are '
                               'too much date distance until next trading day'.format(self.code, self.date))
            return None
        else:
            self._next_trading_date = next_trading_date
            return next_trading_date

    def prepare_data(self):
        if self._data is not None:
            return self._data

        time_offset = timedelta(days=SAMPLE_DATE_OFFSET)
        time_bias = timedelta(days=SAMPLE_DATE_BIAS)
        self._limit_sample_start = self.date - time_offset - time_bias
        self._limit_sample_end = self._limit_sample_start + time_offset
        sql = "SELECT \
            MIN(vol) as vol_min,  AVG(vol) as vol_avg, MAX(vol) as vol_max,  \
            MIN(close) as vol_min,  AVG(close) as vol_avg, MAX(close) as vol_max, \
            MIN(count) as count_min, MAX(count) as count_max \
            FROM {0} \
            WHERE `code`='{1}' AND `time`>='{2}' AND `time`<='{3}' ".format(
            TABLE_NAME_5MIN, self.code, self._limit_sample_start, self._limit_sample_end)
        rs = self.db.execute(sql)
        self._vol_min, self._vol_avg, self._vol_max, \
        self._price_min, self._price_avg, self._price_max, \
        self._count_min, self._count_max = rs.fetchone()

        if self._count_min is None:
            raise RuntimeError("Cannot fetch sample data for stock {} at {}".format(self.code, self.date))
            return

        shifted_date = self._get_shifted_startdate()
        if shifted_date is None:
            return

        rs = self.db.execute(
            "SELECT `date`, ROUND((`traded_market_value`/`close`)) as total_vol "
            "FROM {0} "
            "WHERE `code`='{1}' AND `date`>='{2}' AND `date`<='{3}' "
            "ORDER BY `date` ASC".format(
                TABLE_NAME_DAILY, self.code, shifted_date, self.date))
        daily_df = pd.DataFrame(rs.fetchall())
        daily_df.columns = ['date', 'total_vol']
        self._daily_df = daily_df.set_index(['date'], drop=True)

        rs = self.db.execute(
            "SELECT * "
            "FROM {0} "
            "WHERE `code`='{1}' AND `time`>='{2}' AND `time`<='{3}' "
            "ORDER BY time ASC".format(
                TABLE_NAME_5MIN, self.code, shifted_date, self.date + timedelta(days=1)))
        df = pd.DataFrame(rs.fetchall())
        df.columns = ['code', 'time', 'open', 'high', 'low', 'close', 'vol', 'amount', 'count']
        df = df.set_index(['time'], drop=True)
        df = df.drop(labels='code', axis=1)
        df['date'] = [time.date() for time in df.index.tolist()]
        self._data = df

        return self._data

    def extract_features(self, dup_op="skip"):
        if self._feature_extracted_data is not None:
            return self._feature_extracted_data

        shifted_date = self._get_shifted_startdate()
        if shifted_date is None:
            # 没有适合处理的数据
            return

        # check duplicate
        # 看一下目标表 有没有这只股票当天的记录
        rs = self.db.execute(
            "SELECT COUNT(*) "
            "FROM {0} "
            "WHERE `code`='{1}' AND `time`>='{2}' AND `time`<='{3}'"
                .format(
                TABLE_NAME_5MIN_EXTRACTED, self.code, self.date, self.date + timedelta(days=1)
            )
        )
        data = rs.fetchone()
        rows = int(data[0])
        # rows = 0

        if rows > 0:
            if dup_op == 'skip':
                # 如果遇到重复需要忽略，那就在这里就结束了
                return
            elif dup_op == 'replace':
                # 如果需要替换，那就删掉表里的记录重生成
                sql = "DELETE FROM {0} WHERE `code`='{1}' AND `time`>='{2}' AND `time`<='{3}'".format(
                    TABLE_NAME_5MIN_EXTRACTED, self.code, shifted_date, self.date, self.date + timedelta(days=1)
                )
                self.db.execute(sql)
                self.db.commit()

        df = self.prepare_data()
        if df is None:
            return

        if 'open_change' not in df.columns:
            df = PriceVec.calculate(df)

        if 'ma5' not in df.columns:
            df = PriceMA.calculate(df)

        if 'v_ma5' not in df.columns:
            df = VolMA.calculate(df)

        if 'change' not in df.columns:
            df = PriceChange.calculate(df)

        if 'amplitude' not in df.columns:
            df = PriceAmplitude.calculate(df)

        if 'cci_5' not in df.columns:
            df = CCI.calculate(df)

        if 'rsi_6' not in df.columns:
            df = RSI.calculate(df)

        if 'k' not in df.columns:
            df = KDJ.calculate(df)

        if 'bias_5' not in df.columns:
            df = BIAS.calculate(df)

        if 'boll_md' not in df.columns:
            df = BOLL.calculate(df)

        if 'roc_12' not in df.columns:
            df = ROC.calculate(df)

        if 'vr' not in df.columns:
            df = VR.calculate(df)

        if 'turnover' not in df.columns:
            df = Turnover.calculate(df, self._daily_df)

        if 'wr_5' not in df.columns:
            df = WR.calculate(df)

        if 'mi_5' not in df.columns:
            df = MI.calculate(df)

        if 'oscv' not in df.columns:
            df = OSCV.calculate(df)

        if 'dma_dif' not in df.columns:
            df = DMA.calculate(df)

        if 'emv_emv' not in df.columns:
            df = EMV.calculate(df)

        if 'ema_5' not in df.columns:
            df = EXPMA.calculate(df)

        if 'ar' not in df.columns:
            df = ARBR.calculate(df)

        if 'adx' not in df.columns:
            df = DMI.calculate(df)

        if 'asi_5' not in df.columns:
            df = ASI.calculate(df)

        if 'macd_dif' not in df.columns:
            df = MACD.calculate(df)

        if 'psy' not in df.columns:
            df = PSY.calculate(df)

        if 'wvad' not in df.columns:
            df = WVAD.calculate(df)

        # 重新排序一下列的顺序
        df['code'] = self.code
        df['time'] = df.index

        columns = ['code', 'time'] + df.columns.tolist()[:len(df.columns.tolist()) - 2]
        df = df[columns]
        df = df[df.index > str(self.date)]

        if df.shape[0] != 48:
            raise RuntimeError("{} is has missing data in the day {}".format(self.code, self.date))

        self._feature_extracted_data = df
        df.to_sql(name=TABLE_NAME_5MIN_EXTRACTED, con=config.DB_CONN, if_exists="append", index=False)

        return

    @staticmethod
    def features():
        return ["open_vec", "high_vec", "low_vec", "close_vec",
                "open_change", "high_change", "low_change", "close_change",
                "ma5", "ma15", "ma25", "ma40",
                "ema_5", "ema_15", "ema_25", "ema_40",
                "boll_up", "boll_md", "boll_dn",
                "turnover", "count",
                "vol", "vr", "v_ma5", "v_ma15", "v_ma25", "v_ma40",
                "cci_5", "cci_15", "cci_30",
                "rsi_6", "rsi_12", "rsi_24",
                "k9", "d9", "j9",
                "bias_5", "bias_10", "bias_30",
                "roc_12", "roc_25",
                "change", "amplitude", "amplitude_maxb", "amplitude_maxs",
                "wr_5", "wr_10", "wr_20",
                "mi_5", "mi_10", "mi_20", "mi_30",
                "oscv",
                "dma_dif", "dma_ama",
                "ar", "br",
                "pdi", "mdi", "adx", "adxr",
                "asi_5", "asi_15", "asi_25", "asi_40",
                "macd_dif", "macd_dea", "macd_bar",
                "psy", "psy_ma",
                "emv_emv", "emv_maemv",
                "wvad", "wvad_ma"
                ]

    def feature_scaling(self, dup_op="skip"):

        if self._feature_scaled_data is not None:
            return self._feature_scaled_data

        shifted_date = self._get_shifted_startdate()
        if shifted_date is None:
            # 没有适合处理的数据
            return

        df = self.extract_features()
        if df is None:
            return

        # check duplicate
        # 看一下目标表 有没有这只股票当天的记录
        rs = self.db.execute(
            "SELECT COUNT(*) "
            "FROM {0} "
            "WHERE `code`='{1}' AND `time`>='{2}' AND `time`<='{3}'"
                .format(
                TABLE_NAME_5MIN_SCALED, self.code, self.date, self.date + timedelta(days=1)
            )
        )
        data = rs.fetchone()
        rows = int(data[0])
        # rows = 0

        if rows > 0:
            if dup_op == 'skip':
                # 如果遇到重复需要忽略，那就在这里就结束了
                return
            elif dup_op == 'replace':
                # 如果需要替换，那就删掉表里的记录重生成
                sql = "DELETE FROM {0} WHERE `code`='{1}' AND `time`>='{2}' AND `time`<='{3}'".format(
                    TABLE_NAME_5MIN_SCALED, self.code, shifted_date, self.date, self.date + timedelta(days=1)
                )
                self.db.execute(sql)
                self.db.commit()

        # 价格缩放比
        # 成交量缩放比
        # 振幅/涨幅缩放比
        # 换手率缩放比
        amplitude_scale_rate = 100
        rsi_scale_rate = 0.01
        oscv_scale_rate = 0.01
        wr_scale_rate = 0.04

        df[['open_change']] *= amplitude_scale_rate * 1.5
        df[['high_change']] *= amplitude_scale_rate * 1.5
        df[['low_change']] *= amplitude_scale_rate * 1.5
        df[['close_change']] *= amplitude_scale_rate * 1.5
        df[['open_vec']] *= amplitude_scale_rate * 1.5
        df[['high_vec']] *= amplitude_scale_rate * 1.5
        df[['low_vec']] *= amplitude_scale_rate * 1.5
        df[['close_vec']] *= amplitude_scale_rate * 1.5

        # 下面这组数据应该与收盘价来做缩放
        # 否则这么多维度数据数值都非常接近
        # 缩放算法是 scaled = (value - close) * scale_rate_l2
        for index, row in df.iterrows():
            close = df.loc[index, 'close']
            vol = df.loc[index, 'vol']
            if vol == 0:
                vol = 1
            df.loc[index, 'ma5'] = (df.loc[index, 'ma5'] - close) / close * 100 / 2
            df.loc[index, 'ma15'] = (df.loc[index, 'ma15'] - close) / close * 100 / 2
            df.loc[index, 'ma25'] = (df.loc[index, 'ma25'] - close) / close * 100 / 2
            df.loc[index, 'ma40'] = (df.loc[index, 'ma40'] - close) / close * 100 / 2

            df.loc[index, 'ema_5'] = (df.loc[index, 'ema_5'] - close) / close * 100 / 2
            df.loc[index, 'ema_15'] = (df.loc[index, 'ema_15'] - close) / close * 100 / 2
            df.loc[index, 'ema_25'] = (df.loc[index, 'ema_25'] - close) / close * 100 / 2
            df.loc[index, 'ema_40'] = (df.loc[index, 'ema_40'] - close) / close * 100 / 2

            df.loc[index, 'boll_up'] = (df.loc[index, 'boll_up'] - close) / close * 100 / 2
            df.loc[index, 'boll_dn'] = (df.loc[index, 'boll_dn'] - close) / close * 100 / 2
            df.loc[index, 'boll_md'] = (df.loc[index, 'boll_md'] - close) / close * 100 / 2

            df.loc[index, 'v_ma5'] = (df.loc[index, 'v_ma5'] - vol) / vol / 2
            df.loc[index, 'v_ma15'] = (df.loc[index, 'v_ma15'] - vol) / vol / 2
            df.loc[index, 'v_ma25'] = (df.loc[index, 'v_ma25'] - vol) / vol / 2
            df.loc[index, 'v_ma40'] = (df.loc[index, 'v_ma40'] - vol) / vol / 2

            df.loc[index, 'mi_5'] = df.loc[index, 'mi_5'] / close * 80
            df.loc[index, 'mi_10'] = df.loc[index, 'mi_10'] / close * 80
            df.loc[index, 'mi_20'] = df.loc[index, 'mi_20'] / close * 80
            df.loc[index, 'mi_30'] = df.loc[index, 'mi_30'] / close * 80

        # 最后再把价格计算差值

        df[['change']] *= amplitude_scale_rate
        df[['amplitude']] *= amplitude_scale_rate
        df[['amplitude_maxs']] *= amplitude_scale_rate
        df[['amplitude_maxb']] *= amplitude_scale_rate
        df[['turnover']] *= amplitude_scale_rate * 10
        df[['roc_12']] *= amplitude_scale_rate / 2
        df[['roc_25']] *= amplitude_scale_rate / 2

        df[['count']] = (df[['count']] - self._count_min) / (self._count_max - self._count_min)
        df[['vol']] = (df[['vol']] - self._vol_max) / (self._vol_max - self._vol_min) * 6
        df[['vr']] *= 0.5

        df[['cci_5']] *= 0.003
        df[['cci_15']] *= 0.003
        df[['cci_30']] *= 0.003

        df[['rsi_6']] *= rsi_scale_rate
        df[['rsi_12']] *= rsi_scale_rate
        df[['rsi_24']] *= rsi_scale_rate

        df[['k9']] *= rsi_scale_rate
        df[['d9']] *= rsi_scale_rate
        df[['j9']] *= rsi_scale_rate

        df[['wr_5']] *= wr_scale_rate
        df[['wr_10']] *= wr_scale_rate
        df[['wr_20']] *= wr_scale_rate

        df[['oscv']] *= oscv_scale_rate

        df[['dma_dif']] *= (self._price_max - self._price_min) / self._price_min * 8
        df[['dma_ama']] *= (self._price_max - self._price_min) / self._price_min * 8

        df[['ar']] = (df[['ar']] - 100) * 0.01
        df[['br']] = (df[['br']] - 100) * 0.01

        df[['mdi']] *= 2
        df[['pdi']] *= 2
        df[['adx']] *= 0.5
        df[['adxr']] *= 0.5

        df[['asi_5']] *= 1 / self._price_avg
        df[['asi_15']] *= 1 / self._price_avg
        df[['asi_25']] *= 1 / self._price_avg
        df[['asi_40']] *= 1 / self._price_avg

        df[['macd_bar']] *= 1 / self._price_avg * 100
        df[['macd_dea']] *= 1 / self._price_avg * 100
        df[['macd_dif']] *= 1 / self._price_avg * 100

        df[['psy']] *= 0.01
        df[['psy_ma']] *= 0.01

        df[['emv_emv']] *= (self._price_avg / self._vol_avg)
        df[['emv_maemv']] *= (self._price_avg / self._vol_avg)

        df[['wvad']] = (df[['wvad']] - self._vol_min) / (self._vol_max - self._vol_min) * 2.4
        df[['wvad_ma']] = (df[['wvad_ma']] - self._vol_min) / (self._vol_max - self._vol_min) * 2.4

        # 重新排序一下列的顺序
        columns = ['code', 'time'] + self.features()
        df = df[columns]

        self._feature_scaled_data = df
        df.to_sql(name=TABLE_NAME_5MIN_SCALED, con=config.DB_CONN, if_exists="append", index=False)
        return

    def extract_results(self, dup_op="skip"):

        if self._result_data is not None:
            return self._result_data

        shifted_date = self._get_shifted_startdate()
        if shifted_date is None:
            # 没有适合处理的数据
            return

        next_trading_date = self._get_next_trading_date()
        if next_trading_date is None:
            # 没有适合处理的数据
            return

            # check duplicate
            # 看一下目标表 有没有这只股票当天的记录
        rs = self.db.execute(
            "SELECT COUNT(*) "
            "FROM {0} "
            "WHERE `code`='{1}' AND `date`='{2}'".format(
                TABLE_NAME_5MIN_RESULT, self.code, self.date
            )
        )
        data = rs.fetchone()
        rows = int(data[0])
        # rows = 0

        if rows > 0:
            if dup_op == 'skip':
                # 如果遇到重复需要忽略，那就在这里就结束了
                return
            elif dup_op == 'replace':
                # 如果需要替换，那就删掉表里的记录重生成
                sql = "DELETE FROM {0} WHERE `code`='{1}' AND `date`='{2}'".format(
                    TABLE_NAME_5MIN_RESULT, self.code, shifted_date, self.date
                )
                self.db.execute(sql)
                self.db.commit()

        rs = self.db.execute(
            "SELECT * "
            "FROM {0} "
            "WHERE `code`='{1}' AND `time`>='{2}' AND `time`<='{3}' "
            "ORDER BY time ASC".format(
                TABLE_NAME_5MIN, self.code, self.date, next_trading_date + timedelta(days=1)))
        df = pd.DataFrame(rs.fetchall())
        df.columns = ['code', 'time', 'open', 'high', 'low', 'close', 'vol', 'amount', 'count']

        thisday_df = df[df.time < str(next_trading_date)]
        nextday_df = df[df.time >= str(next_trading_date)]
        thisam_df = df[df.time < datetime.combine(self.date, time.min)
                       + timedelta(hours=12)]
        nextam_df = nextday_df[
            nextday_df.time <= datetime.combine(next_trading_date, time.min)
            + timedelta(hours=12)]
        thispm_df = thisday_df[
            thisday_df.time >= datetime.combine(self.date, time.min)
            + timedelta(hours=12)]

        this_am_close = thisam_df.loc[thisam_df.index[thisam_df.shape[0] - 1], 'close']
        thisday_close = thisday_df.loc[thisday_df.index[thisday_df.shape[0] - 1], 'close']
        nextday_open = nextday_df.loc[nextday_df.index[0], 'open']
        nextday_low = np.min(nextday_df['low'])
        nextday_high = np.max(nextday_df['high'])
        nextday_close = nextday_df.loc[nextday_df.index[nextday_df.shape[0] - 1], 'close']

        this_pm_open_time = datetime.combine(thispm_df.loc[thispm_df.index[0], 'time'].date(), time.min) \
                            + timedelta(hours=13)
        this_pm_low = np.min(thispm_df['low'])
        this_pm_close = thispm_df.loc[thispm_df.index[thispm_df.shape[0] - 1], 'close']
        this_pm_low_time = thispm_df[thispm_df.low == this_pm_low]['time'].tolist()[0]
        this_pm_low_timing = (this_pm_low_time - this_pm_open_time).seconds / 60 / 5  # 1 to 24

        next_am_open = nextam_df.loc[nextam_df.index[0], 'open']
        next_am_open_time = datetime.combine(nextam_df.loc[nextam_df.index[0], 'time'].date(), time.min) \
                            + timedelta(seconds=9.5 * 60 * 60)
        next_am_high = np.max(nextam_df['high'])
        next_am_high_time = nextam_df[nextam_df.high == next_am_high]['time'].tolist()[0]
        next_am_high_timing = (next_am_high_time - next_am_open_time).seconds / 60 / 5  # 1 to 24

        t1_max_profit_rate = (next_am_high - this_pm_low) / this_pm_low * 100  # -10 to +10
        next_am_high_rate = (next_am_high - this_am_close) / this_am_close * 100
        next_am_open_rate = (next_am_open - this_am_close) / this_am_close * 100
        this_pm_low_rate = (this_pm_low - this_am_close) / this_am_close * 100
        this_pm_close_rate = (this_pm_close - this_am_close) / this_am_close * 100

        nextday_open = (nextday_open - thisday_close) / thisday_close * 100
        nextday_high = (nextday_high - thisday_close) / thisday_close * 100
        nextday_low = (nextday_low - thisday_close) / thisday_close * 100
        nextday_close = (nextday_close - thisday_close) / thisday_close * 100

        result_df = pd.DataFrame()
        result_df.loc[0, "code"] = self.code
        result_df.loc[0, "date"] = self.date

        result_df.loc[0, "nextday_open"] = nextday_open
        result_df.loc[0, "nextday_high"] = nextday_high
        result_df.loc[0, "nextday_low"] = nextday_low
        result_df.loc[0, "nextday_close"] = nextday_close

        result_df.loc[0, "this_pm_close_rate"] = this_pm_close_rate
        result_df.loc[0, "this_pm_low_rate"] = this_pm_low_rate
        result_df.loc[0, "this_pm_low_timing"] = this_pm_low_timing

        result_df.loc[0, "next_am_open_rate"] = next_am_open_rate
        result_df.loc[0, "next_am_high_rate"] = next_am_high_rate
        result_df.loc[0, "next_am_high_timing"] = next_am_high_timing

        result_df.loc[0, "t1_max_profit_rate"] = t1_max_profit_rate

        self._result_data = result_df
        result_df.to_sql(name=TABLE_NAME_5MIN_RESULT, con=config.DB_CONN, if_exists="append", index=False)

        return


def process_date_range(start_date, end_date):
    ignored_stock_list = ['sh600000']
    date_diff = end_date - start_date
    session = sessionmaker()
    session.configure(bind=config.DB_CONN)
    s = session()

    print("{} CPUs will be used for processing".format(mp.cpu_count()))
    for diff in range(date_diff.days):
        delta = timedelta(days=diff)
        the_date = start_date + delta

        print("Transforming data: {}\t".format(the_date), end="")
        sql = "SELECT `code` FROM `{}` WHERE `date`='{}' GROUP BY `code`".format(
            TABLE_NAME_DAILY, the_date
        )
        rs = s.execute(sql)
        df = rs.fetchall()

        stock_count = len(df)
        print(" - {} stocks found".format(stock_count))
        if len(df) == 0:
            continue

        pool = mp.Pool()
        pool_res = []

        def callback(res):
            # print(res)
            pool_res.append(res)
            i = len(pool_res)
            if i < stock_count:
                print(">> Processing ... {}%\t\tCode: {} [{}/{}]  \r"
                      .format(round(i / stock_count * 100, 1), code, i, stock_count), end="")
                sys.stdout.flush()
            elif i == stock_count:
                print(" " * 100 + "\r", end="")
                print(">> Processing ... 100%\t[ DONE ]  \r", end="")
                sys.stdout.flush()
                sleep(0.5)
                print(" " * 100 + "\r", end="")
                sys.stdout.flush()
            return

        def ecb(e=None):
            print('get error')
            print(e)
            return

        for i in range(stock_count):
            code = df[i][0]

            if code in ignored_stock_list:
                continue
            # 使用异步进程池处理
            # 每股的处理代码在这里


            pool.apply_async(func=process_single_shot, args=(code, the_date, 'skip'),
                             callback=callback, error_callback=ecb)

        pool.close()
        pool.join()

    s.close()
    return


proc_db = None


def process_single_shot(code, date, dup="skip", db=None):
    own_session = False
    global proc_db
    if proc_db is not None:
        db = proc_db
    elif db is None:
        session = sessionmaker()
        session.configure(bind=config.DB_CONN)
        db = session()
        proc_db = db
        own_session = True

    # 每股的处理代码在这里
    try:
        t = Transform5M(code, date)
        t.db = db
        t.extract_features(dup_op=dup)
        t.feature_scaling(dup_op=dup)
        t.extract_results(dup_op=dup)
    except RuntimeError:
        pass
    except Exception as e:
        print("\n\n\n")
        print("Code: {}\tDate: {}".format(code, date))
        print(e)
        tb = sys.exc_info()[2]
        traceback.print_tb(tb)
        # print(e.with_traceback())
        # exit(0)
        pass
        # 处理代码这里结束

    if own_session == True:
        db.close()
    return code
