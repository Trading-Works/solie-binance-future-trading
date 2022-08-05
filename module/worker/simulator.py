from datetime import datetime, timedelta, timezone
import math
import threading
import multiprocessing
import os
import time
import re
import pickle

import pandas as pd
import numpy as np
from scipy.signal import find_peaks

from module import core
from module import process_toss
from module import thread_toss
from module.recipe import simulate_unit
from module.recipe import make_indicators
from module.recipe import stop_flag
from module.recipe import check_internet
from module.recipe import standardize


class Simulator:
    def __init__(self):
        # ■■■■■ for data management ■■■■■

        self.workerpath = standardize.get_datapath() + "/simulator"
        os.makedirs(self.workerpath, exist_ok=True)
        self.datalocks = [threading.Lock() for _ in range(8)]

        # ■■■■■ remember and display ■■■■■

        self.viewing_symbol = standardize.get_basics()["target_symbols"][0]
        self.should_draw_all_years = False

        self.about_viewing = None

        self.calculation_settings = {
            "year": datetime.now(timezone.utc).year,
            "strategy": 0,
        }
        self.presentation_settings = {
            "maker_fee": 0.02,
            "taker_fee": 0.04,
            "leverage": 1,
        }

        self.raw_account_state = {
            "observed_until": datetime.now(timezone.utc),
            "wallet_balance": 1,
            "positions": {},
            "open_orders": {},
        }
        for symbol in standardize.get_basics()["target_symbols"]:
            self.raw_account_state["positions"][symbol] = {
                "margin": 0,
                "direction": "none",
                "entry_price": 0,
                "update_time": datetime.fromtimestamp(0, tz=timezone.utc),
            }
            self.raw_account_state["open_orders"][symbol] = {}
        self.raw_scribbles = {}
        self.raw_asset_record = pd.DataFrame(
            columns=[
                "Cause",
                "Symbol",
                "Side",
                "Fill Price",
                "Role",
                "Margin Ratio",
                "Order ID",
                "Result Asset",
            ],
            index=pd.DatetimeIndex([], tz="UTC"),
        )
        self.raw_unrealized_changes = pd.Series(
            index=pd.DatetimeIndex([], tz="UTC"), dtype=np.float32
        )

        self.account_state = {
            "observed_until": datetime.now(timezone.utc),
            "wallet_balance": 1,
            "positions": {},
            "open_orders": {},
        }
        for symbol in standardize.get_basics()["target_symbols"]:
            self.account_state["positions"][symbol] = {
                "margin": 0,
                "direction": "none",
                "entry_price": 0,
                "update_time": datetime.fromtimestamp(0, tz=timezone.utc),
            }
            self.account_state["open_orders"][symbol] = {}
        self.scribbles = {}
        self.asset_record = pd.DataFrame(
            columns=[
                "Cause",
                "Symbol",
                "Side",
                "Fill Price",
                "Role",
                "Margin Ratio",
                "Order ID",
                "Result Asset",
            ],
            index=pd.DatetimeIndex([], tz="UTC"),
        )
        self.unrealized_changes = pd.Series(
            index=pd.DatetimeIndex([], tz="UTC"), dtype=np.float32
        )

        text = "No strategy drawn"
        core.window.undertake(lambda t=text: core.window.label_19.setText(t), False)

        # ■■■■■ default executions ■■■■■

        core.window.initialize_functions.append(
            lambda: self.display_lines(),
        )
        core.window.initialize_functions.append(
            lambda: self.display_year_range(),
        )

        # ■■■■■ repetitive schedules ■■■■■

        core.window.scheduler.add_job(
            self.display_available_years,
            trigger="cron",
            second="*",
            executor="thread_pool_executor",
        )
        core.window.scheduler.add_job(
            self.display_lines,
            trigger="cron",
            hour="*",
            executor="thread_pool_executor",
            kwargs={"periodic": True},
        )

        # ■■■■■ websocket streamings ■■■■■

        self.api_streamers = []

        # ■■■■■ invoked by the internet connection  ■■■■■

        connected_functions = []
        check_internet.add_connected_functions(connected_functions)

        disconnected_functions = []
        check_internet.add_disconnected_functions(disconnected_functions)

    def update_viewing_symbol(self, *args, **kwargs):
        def job():
            return core.window.comboBox_6.currentText()

        alias = core.window.undertake(job, True)
        symbol = core.window.alias_to_symbol[alias]
        self.viewing_symbol = symbol

        self.display_lines()

    def update_calculation_settings(self, *args, **kwargs):
        text = core.window.undertake(lambda: core.window.comboBox_5.currentText(), True)
        from_year = self.calculation_settings["year"]
        to_year = int(text)
        self.calculation_settings["year"] = to_year
        if from_year != to_year:
            self.display_year_range()

        index = core.window.undertake(lambda: core.window.comboBox.currentIndex(), True)
        strategy = core.window.strategy_tuples[index][0]
        self.calculation_settings["strategy"] = strategy

        if strategy == 0:
            strategy_details = core.window.strategist.details
        else:
            for strategy_tuple in core.window.strategy_tuples:
                if strategy_tuple[0] == strategy:
                    strategy_details = strategy_tuple[2]
        is_working_strategy = strategy_details[0]

        if not is_working_strategy:
            question = [
                "Strategy not available",
                "Calculation is not available with this strategy.",
                ["Okay"],
                False,
            ]
            core.window.ask(question)

        self.display_lines()

    def update_presentation_settings(self, *args, **kwargs):
        widget = core.window.spinBox_2
        input_value = core.window.undertake(lambda w=widget: w.value(), True)
        self.presentation_settings["leverage"] = input_value
        widget = core.window.doubleSpinBox
        input_value = core.window.undertake(lambda w=widget: w.value(), True)
        self.presentation_settings["taker_fee"] = input_value
        widget = core.window.doubleSpinBox_2
        input_value = core.window.undertake(lambda w=widget: w.value(), True)
        self.presentation_settings["maker_fee"] = input_value
        self.present()

    def display_lines(self, *args, **kwargs):
        # ■■■■■ start the task ■■■■■

        periodic = kwargs.get("periodic", False)
        frequent = kwargs.get("frequent", False)
        only_light_lines = kwargs.get("only_light_lines", False)

        if only_light_lines:
            task_name = "display_light_simulation_lines"
        else:
            task_name = "display_all_simulation_lines"

        task_id = stop_flag.make(task_name)

        # ■■■■■ check frequent drawing ■■■■■

        if frequent:
            pass

        # ■■■■■ check if the data exists ■■■■■

        with core.window.collector.datalocks[0]:
            if len(core.window.collector.candle_data) == 0:
                return

        # ■■■■■ wait for the latest data to be added ■■■■■

        current_moment = datetime.now(timezone.utc).replace(microsecond=0)
        current_moment = current_moment - timedelta(seconds=current_moment.second % 10)
        before_moment = current_moment - timedelta(seconds=10)

        if periodic:
            for _ in range(50):
                if stop_flag.find(task_name, task_id):
                    return
                with core.window.collector.datalocks[0]:
                    last_index = core.window.collector.candle_data.index[-1]
                    if last_index == before_moment:
                        break
                time.sleep(0.1)

        # ■■■■■ get ready for task duration measurement ■■■■■

        pass

        # ■■■■■ check things ■■■■■

        symbol = self.viewing_symbol
        strategy = self.calculation_settings["strategy"]

        # ■■■■■ get light data ■■■■■

        with core.window.collector.datalocks[1]:
            before_chunk = core.window.collector.realtime_data_chunks[-2].copy()
            current_chunk = core.window.collector.realtime_data_chunks[-1].copy()
        realtime_data = np.concatenate((before_chunk, current_chunk))
        with core.window.collector.datalocks[2]:
            aggregate_trades = core.window.collector.aggregate_trades.copy()

        with self.datalocks[0]:
            unrealized_changes = self.unrealized_changes.copy()

        # ■■■■■ draw light lines ■■■■■

        # mark price
        data_x = realtime_data["index"].astype(np.int64) / 10**9
        data_y = realtime_data[str((symbol, "Mark Price"))]
        mask = data_y != 0
        data_y = data_y[mask]
        data_x = data_x[mask]
        widget = core.window.simulation_lines["mark_price"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # last price
        data_x = aggregate_trades["index"].astype(np.int64) / 10**9
        data_y = aggregate_trades[str((symbol, "Price"))]
        mask = data_y != 0
        data_y = data_y[mask]
        data_x = data_x[mask]
        widget = core.window.simulation_lines["last_price"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # last trade volume
        index_ar = aggregate_trades["index"].astype(np.int64) / 10**9
        value_ar = aggregate_trades[str((symbol, "Volume"))]
        mask = value_ar != 0
        index_ar = index_ar[mask]
        value_ar = value_ar[mask]
        length = len(index_ar)
        zero_ar = np.zeros(length)
        nan_ar = np.empty(length)
        nan_ar[:] = np.nan
        data_x = np.repeat(index_ar, 3)
        data_y = np.stack([nan_ar, zero_ar, value_ar], axis=1).reshape(-1)
        widget = core.window.simulation_lines["last_volume"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # book tickers
        data_x = realtime_data["index"].astype(np.int64) / 10**9
        data_y = realtime_data[str((symbol, "Best Bid Price"))]
        mask = data_y != 0
        data_y = data_y[mask]
        data_x = data_x[mask]
        widget = core.window.simulation_lines["book_tickers"][0]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        data_x = realtime_data["index"].astype(np.int64) / 10**9
        data_y = realtime_data[str((symbol, "Best Ask Price"))]
        mask = data_y != 0
        data_y = data_y[mask]
        data_x = data_x[mask]
        widget = core.window.simulation_lines["book_tickers"][1]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # open orders
        boundaries = [
            open_order["boundary"]
            for open_order in self.account_state["open_orders"][symbol].values()
            if "boundary" in open_order
        ]
        first_moment = self.account_state["observed_until"] - timedelta(hours=12)
        last_moment = self.account_state["observed_until"] + timedelta(hours=12)
        for turn, widget in enumerate(core.window.simulation_lines["boundaries"]):
            if turn < len(boundaries):
                boundary = boundaries[turn]
                data_x = np.linspace(
                    first_moment.timestamp(), last_moment.timestamp(), num=1000
                )
                data_y = np.linspace(boundary, boundary, num=1000)
                widget = core.window.simulation_lines["boundaries"][turn]

                def job(widget=widget, data_x=data_x, data_y=data_y):
                    widget.setData(data_x, data_y)

                if stop_flag.find(task_name, task_id):
                    return
                core.window.undertake(job, False)
            else:
                if stop_flag.find(task_name, task_id):
                    return
                core.window.undertake(lambda w=widget: w.clear(), False)

        # entry price
        entry_price = self.account_state["positions"][symbol]["entry_price"]
        first_moment = self.account_state["observed_until"] - timedelta(hours=12)
        last_moment = self.account_state["observed_until"] + timedelta(hours=12)
        if entry_price != 0:
            data_x = np.linspace(
                first_moment.timestamp(), last_moment.timestamp(), num=1000
            )
            data_y = np.linspace(entry_price, entry_price, num=1000)
        else:
            data_x = []
            data_y = []
        widget = core.window.simulation_lines["entry_price"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # ■■■■■ record task duration ■■■■■

        pass

        # ■■■■■ stop if the target is only light lines ■■■■■

        if only_light_lines:
            return

        # ■■■■■ get heavy data ■■■■■

        year = self.calculation_settings["year"]
        slice_until = datetime.now(timezone.utc)
        slice_until = slice_until.replace(minute=0, second=0, microsecond=0)
        slice_until -= timedelta(seconds=1)

        with core.window.collector.datalocks[0]:
            candle_data = core.window.collector.candle_data
            if not self.should_draw_all_years:
                mask = candle_data.index.year == year
                candle_data = candle_data[mask]
            candle_data = candle_data[:slice_until][[symbol]]
            candle_data = candle_data.copy()
        with self.datalocks[1]:
            asset_record = self.asset_record.copy()

        # ■■■■■ maniuplate heavy data ■■■■■

        # add the right end

        if len(candle_data) > 0:
            last_written_moment = candle_data.index[-1]
            new_moment = last_written_moment + timedelta(seconds=10)
            new_index = candle_data.index.union([new_moment])
            candle_data = candle_data.reindex(new_index)

        observed_until = self.account_state["observed_until"]
        if len(asset_record) > 0:
            final_index = asset_record.index[-1]
            final_asset = asset_record.loc[final_index, "Result Asset"]
            asset_record.loc[observed_until, "Cause"] = "other"
            asset_record.loc[observed_until, "Result Asset"] = final_asset
            asset_record = asset_record.sort_index()

        # ■■■■■ make indicators ■■■■■

        indicators_script = core.window.strategist.indicators_script
        compiled_indicators_script = compile(indicators_script, "<string>", "exec")

        indicators = process_toss.apply(
            make_indicators.do,
            candle_data=candle_data,
            strategy=strategy,
            compiled_custom_script=compiled_indicators_script,
        )

        # ■■■■■ draw heavy lines ■■■■■

        # price indicators
        df = indicators[symbol]["Price"]
        data_x = df.index.to_numpy(dtype=np.int64) / 10**9
        data_x += 5
        line_list = core.window.simulation_lines["price_indicators"]
        for turn, widget in enumerate(line_list):
            if turn < len(df.columns):
                column_name = df.columns[turn]
                sr = df[column_name]
                data_y = sr.to_numpy(dtype=np.float32)
                inside_strings = re.findall(r"\(([^)]+)", column_name)
                if len(inside_strings) == 0:
                    color = "#AAAAAA"
                else:
                    color = inside_strings[0]

                def job(widget=widget, data_x=data_x, data_y=data_y, color=color):
                    widget.setPen(color)
                    widget.setData(data_x, data_y)

                if stop_flag.find(task_name, task_id):
                    return
                core.window.undertake(job, False)
            else:
                if stop_flag.find(task_name, task_id):
                    return
                core.window.undertake(lambda w=widget: w.clear(), False)

        # price movement
        index_ar = candle_data.index.to_numpy(dtype=np.int64) / 10**9
        open_ar = candle_data[(symbol, "Open")].to_numpy()
        close_ar = candle_data[(symbol, "Close")].to_numpy()
        high_ar = candle_data[(symbol, "High")].to_numpy()
        low_ar = candle_data[(symbol, "Low")].to_numpy()
        rise_ar = close_ar > open_ar
        length = len(index_ar)
        nan_ar = np.empty(length)
        nan_ar[:] = np.nan

        data_x = np.stack(
            [
                index_ar[rise_ar] + 2,
                index_ar[rise_ar] + 5,
                index_ar[rise_ar],
                index_ar[rise_ar] + 5,
                index_ar[rise_ar] + 8,
                index_ar[rise_ar],
                index_ar[rise_ar] + 5,
                index_ar[rise_ar] + 5,
                index_ar[rise_ar],
            ],
            axis=1,
        ).reshape(-1)
        data_y = np.stack(
            [
                open_ar[rise_ar],
                open_ar[rise_ar],
                nan_ar[rise_ar],
                close_ar[rise_ar],
                close_ar[rise_ar],
                nan_ar[rise_ar],
                high_ar[rise_ar],
                low_ar[rise_ar],
                nan_ar[rise_ar],
            ],
            axis=1,
        ).reshape(-1)
        widget = core.window.simulation_lines["price_up"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        data_x = np.stack(
            [
                index_ar[~rise_ar] + 2,
                index_ar[~rise_ar] + 5,
                index_ar[~rise_ar],
                index_ar[~rise_ar] + 5,
                index_ar[~rise_ar] + 8,
                index_ar[~rise_ar],
                index_ar[~rise_ar] + 5,
                index_ar[~rise_ar] + 5,
                index_ar[~rise_ar],
            ],
            axis=1,
        ).reshape(-1)
        data_y = np.stack(
            [
                open_ar[~rise_ar],
                open_ar[~rise_ar],
                nan_ar[~rise_ar],
                close_ar[~rise_ar],
                close_ar[~rise_ar],
                nan_ar[~rise_ar],
                high_ar[~rise_ar],
                low_ar[~rise_ar],
                nan_ar[~rise_ar],
            ],
            axis=1,
        ).reshape(-1)
        widget = core.window.simulation_lines["price_down"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # wobbles
        sr = candle_data[(symbol, "High")]
        data_x = sr.index.to_numpy(dtype=np.int64) / 10**9
        data_y = sr.to_numpy(dtype=np.float32)
        widget = core.window.simulation_lines["wobbles"][0]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        sr = candle_data[(symbol, "Low")]
        data_x = sr.index.to_numpy(dtype=np.int64) / 10**9
        data_y = sr.to_numpy(dtype=np.float32)
        widget = core.window.simulation_lines["wobbles"][1]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # trade volume indicators
        df = indicators[symbol]["Volume"]
        data_x = df.index.to_numpy(dtype=np.int64) / 10**9
        data_x += 5
        line_list = core.window.simulation_lines["volume_indicators"]
        for turn, widget in enumerate(line_list):
            if turn < len(df.columns):
                column_name = df.columns[turn]
                sr = df[column_name]
                data_y = sr.to_numpy(dtype=np.float32)
                inside_strings = re.findall(r"\(([^)]+)", column_name)
                if len(inside_strings) == 0:
                    color = "#AAAAAA"
                else:
                    color = inside_strings[0]

                def job(widget=widget, data_x=data_x, data_y=data_y, color=color):
                    widget.setPen(color)
                    widget.setData(data_x, data_y)

                if stop_flag.find(task_name, task_id):
                    return
                core.window.undertake(job, False)
            else:
                if stop_flag.find(task_name, task_id):
                    return
                core.window.undertake(lambda w=widget: w.clear(), False)

        # trade volume
        sr = candle_data[(symbol, "Volume")]
        sr = sr.fillna(value=0)
        data_x = sr.index.to_numpy(dtype=np.int64) / 10**9
        data_y = sr.to_numpy(dtype=np.float32)
        widget = core.window.simulation_lines["volume"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # abstract indicators
        df = indicators[symbol]["Abstract"]
        data_x = df.index.to_numpy(dtype=np.int64) / 10**9
        data_x += 5
        line_list = core.window.simulation_lines["abstract_indicators"]
        for turn, widget in enumerate(line_list):
            if turn < len(df.columns):
                column_name = df.columns[turn]
                sr = df[column_name]
                data_y = sr.to_numpy(dtype=np.float32)
                inside_strings = re.findall(r"\(([^)]+)", column_name)
                if len(inside_strings) == 0:
                    color = "#AAAAAA"
                else:
                    color = inside_strings[0]

                def job(widget=widget, data_x=data_x, data_y=data_y, color=color):
                    widget.setPen(color)
                    widget.setData(data_x, data_y)

                if stop_flag.find(task_name, task_id):
                    return
                core.window.undertake(job, False)
            else:
                if stop_flag.find(task_name, task_id):
                    return
                core.window.undertake(lambda w=widget: w.clear(), False)

        # asset
        data_x = asset_record["Result Asset"].index.to_numpy(dtype=np.int64) / 10**9
        data_y = asset_record["Result Asset"].to_numpy(dtype=np.float32)
        widget = core.window.simulation_lines["asset"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # asset with unrealized profit
        if len(asset_record) >= 2:
            sr = asset_record["Result Asset"].resample("10S").ffill()
        unrealized_changes_sr = unrealized_changes.reindex(sr.index)
        sr = sr * (1 + unrealized_changes_sr)
        data_x = sr.index.to_numpy(dtype=np.int64) / 10**9 + 5
        data_y = sr.to_numpy(dtype=np.float32)
        widget = core.window.simulation_lines["asset_with_unrealized_profit"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # buy and sell
        df = asset_record.loc[asset_record["Symbol"] == symbol]
        df = df[df["Side"] == "sell"]
        sr = df["Fill Price"]
        data_x = sr.index.to_numpy(dtype=np.int64) / 10**9
        data_y = sr.to_numpy(dtype=np.float32)
        widget = core.window.simulation_lines["sell"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        df = asset_record.loc[asset_record["Symbol"] == symbol]
        df = df[df["Side"] == "buy"]
        sr = df["Fill Price"]
        data_x = sr.index.to_numpy(dtype=np.int64) / 10**9
        data_y = sr.to_numpy(dtype=np.float32)
        widget = core.window.simulation_lines["buy"]

        def job(widget=widget, data_x=data_x, data_y=data_y):
            widget.setData(data_x, data_y)

        if stop_flag.find(task_name, task_id):
            return
        core.window.undertake(job, False)

        # ■■■■■ record task duration ■■■■■

        pass

    def erase(self, *args, **kwargs):
        self.raw_account_state = {
            "observed_until": datetime.now(timezone.utc),
            "wallet_balance": 1,
            "positions": {},
            "open_orders": {},
        }
        for symbol in standardize.get_basics()["target_symbols"]:
            self.raw_account_state["positions"][symbol] = {
                "margin": 0,
                "direction": "none",
                "entry_price": 0,
                "update_time": datetime.fromtimestamp(0, tz=timezone.utc),
            }
            self.raw_account_state["open_orders"][symbol] = {}
        self.raw_scribbles = {}
        self.raw_asset_record = pd.DataFrame(
            columns=[
                "Cause",
                "Symbol",
                "Side",
                "Fill Price",
                "Role",
                "Margin Ratio",
                "Order ID",
                "Result Asset",
            ],
            index=pd.DatetimeIndex([], tz="UTC"),
        )
        self.raw_unrealized_changes = pd.Series(
            index=pd.DatetimeIndex([], tz="UTC"), dtype=np.float32
        )
        self.about_viewing = None

        self.present()

    def display_available_years(self, *args, **kwargs):
        with core.window.collector.datalocks[0]:
            years_sr = core.window.collector.candle_data.index.year.drop_duplicates()
        years = years_sr.tolist()
        years.sort(reverse=True)
        years = [str(year) for year in years]

        def job():
            widget = core.window.comboBox_5
            return [int(widget.itemText(i)) for i in range(widget.count())]

        choices = core.window.undertake(job, True)
        choices.sort(reverse=True)
        choices = [str(choice) for choice in choices]

        if years != choices:
            # if it's changed
            widget = core.window.comboBox_5
            core.window.undertake(lambda w=widget: w.clear(), False)
            core.window.undertake(lambda w=widget, y=years: w.addItems(y), False)

    def simulate_only_visible(self, *args, **kwargs):
        self.calculate(only_visible=True)

    def display_range_information(self, *args, **kwargs):
        task_id = stop_flag.make("display_simulation_range_information")

        symbol = self.viewing_symbol

        range_start = core.window.undertake(
            lambda: core.window.plot_widget_2.getAxis("bottom").range[0], True
        )
        range_start = max(range_start, 0)
        range_start = datetime.fromtimestamp(range_start, tz=timezone.utc)

        if stop_flag.find("display_simulation_range_information", task_id):
            return

        range_end = core.window.undertake(
            lambda: core.window.plot_widget_2.getAxis("bottom").range[1], True
        )
        if range_end < 0:
            # case when pyqtgraph passed negative value because it's too big
            range_end = 9223339636
        else:
            # maximum value available in pandas
            range_end = min(range_end, 9223339636)
        range_end = datetime.fromtimestamp(range_end, tz=timezone.utc)

        if stop_flag.find("display_simulation_range_information", task_id):
            return

        range_length = range_end - range_start
        range_days = range_length.days
        range_hours, remains = divmod(range_length.seconds, 3600)
        range_minutes, remains = divmod(remains, 60)
        range_length_text = f"{range_days}d {range_hours}h {range_minutes}s"

        if stop_flag.find("display_simulation_range_information", task_id):
            return

        with self.datalocks[0]:
            unrealized_changes = self.unrealized_changes[range_start:range_end].copy()
        with self.datalocks[1]:
            asset_record = self.asset_record[range_start:range_end].copy()

        asset_changes = asset_record["Result Asset"].pct_change() + 1
        asset_changes = asset_changes.reindex(asset_record.index).fillna(value=1)
        symbol_mask = asset_record["Symbol"] == symbol

        # trade count
        total_change_count = len(asset_changes)
        symbol_change_count = len(asset_changes[symbol_mask])
        # trade volume
        if len(asset_record) > 0:
            total_margin_ratio = asset_record["Margin Ratio"].sum()
        else:
            total_margin_ratio = 0
        if len(asset_record[symbol_mask]) > 0:
            symbol_margin_ratio = asset_record[symbol_mask]["Margin Ratio"].sum()
        else:
            symbol_margin_ratio = 0
        # asset changes
        if len(asset_changes) > 0:
            total_yield = asset_changes.cumprod().iloc[-1]
            total_yield = (total_yield - 1) * 100
        else:
            total_yield = 0
        if len(asset_changes[symbol_mask]) > 0:
            symbol_yield = asset_changes[symbol_mask].cumprod().iloc[-1]
            symbol_yield = (symbol_yield - 1) * 100
        else:
            symbol_yield = 0
        # least unrealized changes
        if len(unrealized_changes) > 0:
            min_unrealized_change = unrealized_changes.min()
        else:
            min_unrealized_change = 0

        if stop_flag.find("display_simulation_range_information", task_id):
            return

        range_down = core.window.undertake(
            lambda: core.window.plot_widget_2.getAxis("left").range[0], True
        )
        range_up = core.window.undertake(
            lambda: core.window.plot_widget_2.getAxis("left").range[1], True
        )
        range_height = round((1 - range_down / range_up) * 100, 2)

        text = ""
        text += f"Visible time range {range_length_text}"
        text += "  ⦁  "
        text += f"Visible price range {range_height}%"
        text += "  ⦁  "
        text += f"Transaction count {symbol_change_count}/{total_change_count}"
        text += "  ⦁  "
        text += (
            "Transaction amount"
            f" ×{round(symbol_margin_ratio,4)}/{round(total_margin_ratio,4)}"
        )
        text += "  ⦁  "
        text += f"Total realized profit {round(symbol_yield,4)}/{round(total_yield,4)}%"
        text += "  ⦁  "
        text += f"Lowest unrealized profit {round(min_unrealized_change*100,2)}%"
        core.window.undertake(lambda t=text: core.window.label_13.setText(t), False)

    def set_minimum_view_range(self, *args, **kwargs):
        def job():
            range_down = core.window.plot_widget_2.getAxis("left").range[0]
            core.window.plot_widget_2.plotItem.vb.setLimits(
                minYRange=range_down * 0.005
            )
            range_down = core.window.plot_widget_3.getAxis("left").range[0]
            core.window.plot_widget_3.plotItem.vb.setLimits(
                minYRange=range_down * 0.005
            )

        core.window.undertake(job, False)

    def calculate(self, *args, **kwargs):
        task_id = stop_flag.make("calculate_simulation")

        only_visible = kwargs.get("only_visible", False)

        prepare_step = 0
        calculate_step = 0

        def job():
            while True:
                if stop_flag.find("calculate_simulation", task_id):
                    widget = core.window.progressBar_4
                    core.window.undertake(lambda w=widget: w.setValue(0), False)
                    widget = core.window.progressBar
                    core.window.undertake(lambda w=widget: w.setValue(0), False)
                    return
                else:
                    if prepare_step == 6 and calculate_step == 1000:
                        is_progressbar_filled = True
                        progressbar_value = core.window.undertake(
                            lambda: core.window.progressBar_4.value(), True
                        )
                        if progressbar_value < 1000:
                            is_progressbar_filled = False
                        progressbar_value = core.window.undertake(
                            lambda: core.window.progressBar.value(), True
                        )
                        if progressbar_value < 1000:
                            is_progressbar_filled = False
                        if is_progressbar_filled:
                            time.sleep(0.1)
                            widget = core.window.progressBar_4
                            core.window.undertake(lambda w=widget: w.setValue(0), False)
                            widget = core.window.progressBar
                            core.window.undertake(lambda w=widget: w.setValue(0), False)
                            return
                    widget = core.window.progressBar_4
                    before_value = core.window.undertake(
                        lambda w=widget: w.value(), True
                    )
                    if before_value < 1000:
                        remaining = math.ceil(1000 / 6 * prepare_step) - before_value
                        new_value = before_value + math.ceil(remaining * 0.2)
                        core.window.undertake(
                            lambda w=widget, v=new_value: w.setValue(v), False
                        )
                    widget = core.window.progressBar
                    before_value = core.window.undertake(
                        lambda w=widget: w.value(), True
                    )
                    if before_value < 1000:
                        remaining = calculate_step - before_value
                        new_value = before_value + math.ceil(remaining * 0.2)
                        core.window.undertake(
                            lambda w=widget, v=new_value: w.setValue(v), False
                        )
                    time.sleep(0.01)

        thread_toss.apply_async(job)

        prepare_step = 1

        # ■■■■■ default values and the strategy ■■■■■

        year = self.calculation_settings["year"]
        strategy = self.calculation_settings["strategy"]

        asset_record_filepath = (
            f"{self.workerpath}/{strategy}_{year}_asset_record.pickle"
        )
        unrealized_changes_filepath = (
            f"{self.workerpath}/{strategy}_{year}_unrealized_changes.pickle"
        )
        scribbles_filepath = f"{self.workerpath}/{strategy}_{year}_scribbles.pickle"
        account_state_filepath = (
            f"{self.workerpath}/{strategy}_{year}_account_state.pickle"
        )
        virtual_state_filepath = (
            f"{self.workerpath}/{strategy}_{year}_virtual_state.pickle"
        )

        if strategy == 0:
            strategy_details = core.window.strategist.details
        else:
            for strategy_tuple in core.window.strategy_tuples:
                if strategy_tuple[0] == strategy:
                    strategy_details = strategy_tuple[2]
        is_working_strategy = strategy_details[0]
        should_parallalize = strategy_details[1]
        unit_length = strategy_details[2]

        if not is_working_strategy:
            stop_flag.make("calculate_simulation")
            question = [
                "Strategy not available",
                "Choose a different one.",
                ["Okay"],
                False,
            ]
            core.window.ask(question)
            return

        prepare_step = 2

        # ■■■■■ candle data of the year ■■■■■

        # get only year range
        with core.window.collector.datalocks[0]:
            df = core.window.collector.candle_data
            year_candle_data = df[df.index.year == year].copy()
        # slice until last hour
        slice_until = year_candle_data.index[-1] + timedelta(seconds=10)
        slice_until = slice_until.replace(minute=0, second=0, microsecond=0)
        slice_until -= timedelta(seconds=1)
        year_candle_data = year_candle_data[:slice_until]
        # interpolate
        year_candle_data = year_candle_data.interpolate()

        prepare_step = 3

        # ■■■■■ prepare data and calculation range ■■■■■

        blank_asset_record = pd.DataFrame(
            columns=[
                "Cause",
                "Symbol",
                "Side",
                "Fill Price",
                "Role",
                "Margin Ratio",
                "Order ID",
                "Result Asset",
            ],
            index=pd.DatetimeIndex([], tz="UTC"),
        )
        blank_unrealized_changes = pd.Series(
            index=pd.DatetimeIndex([], tz="UTC"), dtype=np.float32
        )
        blank_scribbles = {}
        blank_account_state = {
            "observed_until": datetime.now(timezone.utc),
            "wallet_balance": 1,
            "positions": {},
            "open_orders": {},
        }
        for symbol in standardize.get_basics()["target_symbols"]:
            blank_account_state["positions"][symbol] = {
                "margin": 0,
                "direction": "none",
                "entry_price": 0,
                "update_time": datetime.fromtimestamp(0, tz=timezone.utc),
            }
            blank_account_state["open_orders"][symbol] = {}
        blank_virtual_state = {
            "available_balance": 1,
            "locations": {},
            "placements": {},
        }
        for symbol in standardize.get_basics()["target_symbols"]:
            blank_virtual_state["locations"][symbol] = {
                "amount": 0,
                "entry_price": 0,
            }
            blank_virtual_state["placements"][symbol] = {}

        prepare_step = 4

        if only_visible:
            # when calculating only visible range

            previous_asset_record = blank_asset_record.copy()
            previous_unrealized_changes = blank_unrealized_changes.copy()
            previous_scribbles = blank_scribbles.copy()
            previous_account_state = blank_account_state.copy()
            previous_virtual_state = blank_virtual_state.copy()

            range_start = core.window.undertake(
                lambda: core.window.plot_widget_2.getAxis("bottom").range[0], True
            )
            range_start = datetime.fromtimestamp(range_start, tz=timezone.utc)
            range_start = range_start.replace(microsecond=0)
            range_start = range_start - timedelta(seconds=range_start.second % 10)

            range_end = core.window.undertake(
                lambda: core.window.plot_widget_2.getAxis("bottom").range[1], True
            )
            range_end = datetime.fromtimestamp(range_end, tz=timezone.utc)
            range_end = range_end.replace(microsecond=0)
            range_end = range_end - timedelta(seconds=range_end.second % 10)
            range_end += timedelta(seconds=10)

            calculate_from = max(range_start, year_candle_data.index[0])
            calculate_until = min(range_end, year_candle_data.index[-1])

        else:
            # when calculating properly
            try:
                previous_asset_record = pd.read_pickle(asset_record_filepath)
                previous_unrealized_changes = pd.read_pickle(
                    unrealized_changes_filepath
                )
                with open(scribbles_filepath, "rb") as file:
                    previous_scribbles = pickle.load(file)
                with open(account_state_filepath, "rb") as file:
                    previous_account_state = pickle.load(file)
                with open(virtual_state_filepath, "rb") as file:
                    previous_virtual_state = pickle.load(file)

                calculate_from = previous_account_state["observed_until"]
                calculate_until = year_candle_data.index[-1]
            except FileNotFoundError:
                previous_asset_record = blank_asset_record.copy()
                previous_unrealized_changes = blank_unrealized_changes.copy()
                previous_scribbles = blank_scribbles.copy()
                previous_account_state = blank_account_state.copy()
                previous_virtual_state = blank_virtual_state.copy()

                calculate_from = year_candle_data.index[0]
                calculate_until = year_candle_data.index[-1]

        should_calculate = calculate_from < calculate_until
        if len(previous_asset_record) == 0:
            previous_asset_record.loc[calculate_from, "Cause"] = "other"
            previous_asset_record.loc[calculate_from, "Result Asset"] = float(1)

        prepare_step = 5

        # ■■■■■ prepare per unit data ■■■■■

        if should_calculate:
            decision_script = core.window.strategist.decision_script
            indicators_script = core.window.strategist.indicators_script
            compiled_indicators_script = compile(indicators_script, "<string>", "exec")

            slice_from = calculate_from - timedelta(days=7)
            # a little more data for generation
            slice_to = calculate_until
            year_indicators = process_toss.apply(
                make_indicators.do,
                candle_data=year_candle_data[slice_from:slice_to],
                strategy=strategy,
                compiled_custom_script=compiled_indicators_script,
            )

            if should_parallalize:
                needed_candle_data = year_candle_data[calculate_from:calculate_until]
                division = timedelta(days=unit_length)
                unit_candle_data_list = [
                    unit_candle_data
                    for _, unit_candle_data in needed_candle_data.groupby(
                        pd.Grouper(freq=division, origin="epoch")
                    )
                ]

                communication_manager = multiprocessing.Manager()
                unit_count = len(unit_candle_data_list)
                progress_list = communication_manager.list([0] * unit_count)

                input_data = []
                for turn, unit_candle_data in enumerate(unit_candle_data_list):
                    base_index = unit_candle_data.index
                    unit_indicators = year_indicators.reindex(base_index)
                    get_from = base_index[0]
                    get_to = base_index[-1] + timedelta(seconds=10)
                    unit_asset_record = previous_asset_record[get_from:get_to]
                    unit_unrealized_changes = previous_unrealized_changes[
                        get_from:get_to
                    ]
                    if get_from < calculate_from <= get_to:
                        unit_scribbles = previous_scribbles
                        unit_account_state = previous_account_state
                        unit_virtual_state = previous_virtual_state
                    else:
                        unit_scribbles = blank_scribbles
                        unit_account_state = blank_account_state
                        unit_virtual_state = blank_virtual_state

                    dataset = {
                        "progress_list": progress_list,
                        "target_progress": turn,
                        "strategy": strategy,
                        "unit_candle_data": unit_candle_data,
                        "unit_indicators": unit_indicators,
                        "unit_asset_record": unit_asset_record,
                        "unit_unrealized_changes": unit_unrealized_changes,
                        "unit_scribbles": unit_scribbles,
                        "unit_account_state": unit_account_state,
                        "unit_virtual_state": unit_virtual_state,
                        "calculate_from": calculate_from,
                        "calculate_until": calculate_until,
                        "decision_script": decision_script,
                    }
                    input_data.append(dataset)

            else:
                communication_manager = multiprocessing.Manager()
                progress_list = communication_manager.list([0])

                input_data = []
                dataset = {
                    "progress_list": progress_list,
                    "target_progress": 0,
                    "strategy": strategy,
                    "unit_candle_data": year_candle_data,
                    "unit_indicators": year_indicators,
                    "unit_asset_record": previous_asset_record,
                    "unit_unrealized_changes": previous_unrealized_changes,
                    "unit_scribbles": previous_scribbles,
                    "unit_account_state": previous_account_state,
                    "unit_virtual_state": previous_virtual_state,
                    "calculate_from": calculate_from,
                    "calculate_until": calculate_until,
                    "decision_script": decision_script,
                }
                input_data.append(dataset)

        prepare_step = 6

        # ■■■■■ calculate ■■■■■

        if should_calculate:
            map_result = process_toss.map_async(simulate_unit.do, input_data)

            total_seconds = (calculate_until - calculate_from).total_seconds()
            while True:
                if map_result.ready():
                    if map_result.successful():
                        output_data = map_result.get()
                        break
                    else:
                        stop_flag.make("calculate_simulation")
                if stop_flag.find("calculate_simulation", task_id):
                    return
                total_progress = sum(progress_list)
                calculate_step = math.ceil(total_progress * 1000 / total_seconds)

        calculate_step = 1000

        # ■■■■■ get calculation result ■■■■■

        if should_calculate:
            asset_record = previous_asset_record
            for unit_ouput_data in output_data:
                unit_asset_record = unit_ouput_data["unit_asset_record"]
                concat_data = [asset_record, unit_asset_record]
                asset_record = pd.concat(concat_data)
            mask = ~asset_record.index.duplicated()
            asset_record = asset_record[mask]
            asset_record = asset_record.sort_index()

            unrealized_changes = previous_unrealized_changes
            for unit_ouput_data in output_data:
                unit_unrealized_changes = unit_ouput_data["unit_unrealized_changes"]
                concat_data = [unrealized_changes, unit_unrealized_changes]
                unrealized_changes = pd.concat(concat_data)
            mask = ~unrealized_changes.index.duplicated()
            unrealized_changes = unrealized_changes[mask]
            unrealized_changes = unrealized_changes.sort_index()

            scribbles = output_data[-1]["unit_scribbles"]
            account_state = output_data[-1]["unit_account_state"]
            virtual_state = output_data[-1]["unit_virtual_state"]

        else:
            asset_record = previous_asset_record
            unrealized_changes = previous_unrealized_changes
            scribbles = previous_scribbles
            account_state = previous_account_state

        # ■■■■■ remember and present ■■■■■

        self.raw_asset_record = asset_record
        self.raw_unrealized_changes = unrealized_changes
        self.raw_scribbles = scribbles
        self.raw_account_state = account_state
        self.about_viewing = {"year": year, "strategy": strategy}
        self.present()

        # ■■■■■ save if properly calculated ■■■■■

        if not only_visible and should_calculate:
            asset_record.to_pickle(asset_record_filepath)
            unrealized_changes.to_pickle(unrealized_changes_filepath)
            with open(scribbles_filepath, "wb") as file:
                pickle.dump(scribbles, file)
            with open(account_state_filepath, "wb") as file:
                pickle.dump(account_state, file)
            with open(virtual_state_filepath, "wb") as file:
                pickle.dump(virtual_state, file)

    def present(self, *args, **kwargs):
        maker_fee = self.presentation_settings["maker_fee"]
        taker_fee = self.presentation_settings["taker_fee"]
        leverage = self.presentation_settings["leverage"]

        with self.datalocks[0]:
            asset_record = self.raw_asset_record.copy()
            unrealized_changes = self.raw_unrealized_changes.copy()
            scribbles = self.raw_scribbles.copy()
            account_state = self.raw_account_state.copy()

        # ■■■■■ get strategy details ■■■■

        if self.about_viewing is None:
            should_parallalize = False
            unit_length = 0
        else:
            strategy = self.about_viewing["strategy"]
            if strategy == 0:
                strategy_details = core.window.strategist.details
            else:
                for strategy_tuple in core.window.strategy_tuples:
                    if strategy_tuple[0] == strategy:
                        strategy_details = strategy_tuple[2]
            should_parallalize = strategy_details[1]
            unit_length = strategy_details[2]

        # ■■■■■ apply other factors to the asset trace ■■■■

        if should_parallalize:
            division = timedelta(days=unit_length)
            grouped = asset_record.groupby(pd.Grouper(freq=division, origin="epoch"))
            unit_asset_record_list = [r.dropna() for _, r in grouped]
            unit_count = len(unit_asset_record_list)

        else:
            unit_asset_record_list = [asset_record]
            unit_count = 1

        unit_asset_changes_list = []
        for turn in range(unit_count):
            unit_asset_record = unit_asset_record_list[turn]

            # leverage
            unit_result_asset_sr = unit_asset_record["Result Asset"]
            unit_asset_shifts = unit_result_asset_sr.diff()
            if len(unit_asset_shifts) > 0:
                unit_asset_shifts.iloc[0] = 0
            lazy_unit_result_asset = unit_result_asset_sr.shift(periods=1)
            if len(lazy_unit_result_asset) > 0:
                lazy_unit_result_asset.iloc[0] = 1
            unit_asset_changes_by_leverage = (
                1 + unit_asset_shifts / lazy_unit_result_asset * leverage
            )

            # fee
            unit_fees = unit_asset_record["Role"].copy()
            unit_fees[unit_fees == "maker"] = maker_fee
            unit_fees[unit_fees == "taker"] = taker_fee
            unit_fees = unit_fees.astype(np.float32)
            unit_margin_ratios = unit_asset_record["Margin Ratio"]
            unit_asset_changes_by_fee = (
                1 - (unit_fees / 100) * unit_margin_ratios * leverage
            )

            # altogether
            unit_asset_changes = (
                unit_asset_changes_by_leverage * unit_asset_changes_by_fee
            )
            unit_asset_changes_list.append(unit_asset_changes)

        unrealized_changes = unrealized_changes * leverage

        year_asset_changes = pd.concat(unit_asset_changes_list).sort_index()
        if len(year_asset_changes) > 0:
            year_asset_changes.iloc[0] = float(1)
        asset_record = asset_record.reindex(year_asset_changes.index)
        asset_record["Result Asset"] = year_asset_changes.cumprod()

        presentation_asset_record = asset_record.copy()
        presentation_unrealized_changes = unrealized_changes.copy()
        presentation_scribbles = scribbles.copy()
        presentation_account_state = account_state.copy()

        # ■■■■■ remember ■■■■■

        self.scribbles = presentation_scribbles
        self.account_state = presentation_account_state
        with self.datalocks[0]:
            self.unrealized_changes = presentation_unrealized_changes
        with self.datalocks[1]:
            self.asset_record = presentation_asset_record

        # ■■■■■ display ■■■■■

        self.display_lines()
        self.display_range_information()

        if self.about_viewing is None:
            text = "No strategy drawn"
            core.window.undertake(lambda t=text: core.window.label_19.setText(t), False)
        else:
            year = self.about_viewing["year"]
            strategy = self.about_viewing["strategy"]
            text = ""
            text += f"Target year {year}"
            text += "  ⦁  "
            text += f"Strategy number {strategy}"
            core.window.undertake(lambda t=text: core.window.label_19.setText(t), False)

    def display_year_range(self, *args, **kwargs):
        range_start = datetime(
            year=self.calculation_settings["year"],
            month=1,
            day=1,
            tzinfo=timezone.utc,
        )
        range_start = range_start.timestamp()
        range_end = datetime(
            year=self.calculation_settings["year"] + 1,
            month=1,
            day=1,
            tzinfo=timezone.utc,
        )
        range_end = range_end.timestamp()
        widget = core.window.plot_widget_2

        def job(range_start=range_start, range_end=range_end):
            widget.setXRange(range_start, range_end)

        core.window.undertake(job, False)

    def delete_calculation_data(self, *args, **kwargs):
        year = self.calculation_settings["year"]
        strategy = self.calculation_settings["strategy"]

        asset_record_filepath = (
            f"{self.workerpath}/{strategy}_{year}_asset_record.pickle"
        )
        unrealized_changes_filepath = (
            f"{self.workerpath}/{strategy}_{year}_unrealized_changes.pickle"
        )
        scribbles_filepath = f"{self.workerpath}/{strategy}_{year}_scribbles.pickle"
        account_state_filepath = (
            f"{self.workerpath}/{strategy}_{year}_account_state.pickle"
        )
        virtual_state_filepath = (
            f"{self.workerpath}/{strategy}_{year}_virtual_state.pickle"
        )

        does_file_exist = False

        if os.path.exists(asset_record_filepath):
            does_file_exist = True
        if os.path.exists(unrealized_changes_filepath):
            does_file_exist = True
        if os.path.exists(scribbles_filepath):
            does_file_exist = True
        if os.path.exists(account_state_filepath):
            does_file_exist = True
        if os.path.exists(virtual_state_filepath):
            does_file_exist = True

        if not does_file_exist:
            question = [
                f"No calculation data on year {year} with strategy number {strategy}",
                "You should calculate first.",
                ["Okay"],
                False,
            ]
            core.window.ask(question)
            return
        else:
            question = [
                f"Are you sure you want to delete calculation data on year {year} with"
                f" strategy number {strategy}?",
                "If you do, you should perform the calculation again to see the"
                " prediction of the strategy. Calculation data of other combinations"
                " does not get affected.",
                ["Cancel", "Delete"],
                False,
            ]
            answer = core.window.ask(question)
            if answer in (0, 1):
                return

        try:
            os.remove(asset_record_filepath)
        except FileNotFoundError:
            pass
        try:
            os.remove(unrealized_changes_filepath)
        except FileNotFoundError:
            pass
        try:
            os.remove(scribbles_filepath)
        except FileNotFoundError:
            pass
        try:
            os.remove(account_state_filepath)
        except FileNotFoundError:
            pass
        try:
            os.remove(virtual_state_filepath)
        except FileNotFoundError:
            pass

        self.erase()

    def draw(self, *args, **kwargs):
        year = self.calculation_settings["year"]
        strategy = self.calculation_settings["strategy"]

        asset_record_filepath = (
            f"{self.workerpath}/{strategy}_{year}_asset_record.pickle"
        )
        unrealized_changes_filepath = (
            f"{self.workerpath}/{strategy}_{year}_unrealized_changes.pickle"
        )
        scribbles_filepath = f"{self.workerpath}/{strategy}_{year}_scribbles.pickle"
        account_state_filepath = (
            f"{self.workerpath}/{strategy}_{year}_account_state.pickle"
        )

        try:
            with self.datalocks[0]:
                self.raw_asset_record = pd.read_pickle(asset_record_filepath)
                self.raw_unrealized_changes = pd.read_pickle(
                    unrealized_changes_filepath
                )
                with open(scribbles_filepath, "rb") as file:
                    self.raw_scribbles = pickle.load(file)
                with open(account_state_filepath, "rb") as file:
                    self.raw_account_state = pickle.load(file)
            self.about_viewing = {"year": year, "strategy": strategy}
            self.present()
        except FileNotFoundError:
            question = [
                f"No calculation data on year {year} with strategy number {strategy}",
                "You should calculate first.",
                ["Okay"],
                False,
            ]
            core.window.ask(question)
            return

    def match_graph_range(self, *args, **kwargs):
        range_start = core.window.undertake(
            lambda: core.window.plot_widget.getAxis("bottom").range[0], True
        )
        range_end = core.window.undertake(
            lambda: core.window.plot_widget.getAxis("bottom").range[1], True
        )
        widget = core.window.plot_widget_2

        def job(range_start=range_start, range_end=range_end):
            widget.setXRange(range_start, range_end, padding=0)

        core.window.undertake(job, False)

    def stop_calculation(self, *args, **kwargs):
        stop_flag.make("calculate_simulation")

    def analyze_unrealized_peaks(self, *args, **kwargs):
        peak_indexes, _ = find_peaks(-self.unrealized_changes, distance=3600 / 10)
        peak_sr = self.unrealized_changes.iloc[peak_indexes]
        peak_sr = peak_sr.sort_values().iloc[:12]
        if len(peak_sr) < 12:
            question = [
                "Calculation data is either missing or too short",
                "Cannot get the list of meaningful spots with lowest unrealized"
                " profit.",
                ["Okay"],
                False,
            ]
            core.window.ask(question)
        else:
            text_lines = [
                str(index) + " " + str(round(peak_value * 100, 2)) + "%"
                for index, peak_value in peak_sr.iteritems()
            ]
            question = [
                "Spots with lowest unrealized profit",
                "\n".join(text_lines),
                ["Okay"],
                False,
            ]
            core.window.ask(question)

    def toggle_combined_draw(self, *args, **kwargs):
        is_checked = args[0]
        if is_checked:
            self.should_draw_all_years = True
        else:
            self.should_draw_all_years = False
        self.display_lines()
