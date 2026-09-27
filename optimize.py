"""
optimize.py — Optimisation Optuna des hyperparamètres du bot.

Usage :
    python optimize.py                            # 50 trials, SOL/EUR, 30j
    python optimize.py --symbol ETH/EUR --trials 100 --days 60
  python optimize.py --show                     # afficher le meilleur résultat existant

L'étude est persistée dans optuna_study.db — relancer ajoute des trials.

Paramètres optimisés :
  rsi_period, atr_period, atr_sl_mult, atr_tp1_mult, atr_tp2_mult,
  buy_threshold, trailing_dist, trade_size_pct, bb_period
"""
import argparse, os, sys
import optuna
from backtest import run_backtest

optuna.logging.set_verbosity(optuna.logging.WARNING)

STUDY_DB  = f"sqlite:///{os.path.join(os.path.dirname(__file__), 'optuna_study.db')}"
STUDY_NAME = "trading_bot_v2"


def objective(trial: optuna.Trial, symbol: str, days: int) -> float:
    params = {
        "rsi_period":    trial.suggest_int(  "rsi_period",    8,  21),
        "atr_period":    trial.suggest_int(  "atr_period",    7,  21),
        "bb_period":     trial.suggest_int(  "bb_period",    14,  30),
        "adx_period":    trial.suggest_int(  "adx_period",    7,  21),
        "atr_sl_mult":   trial.suggest_float("atr_sl_mult",  1.0, 2.5),
        "atr_tp1_mult":  trial.suggest_float("atr_tp1_mult", 0.5, 2.0),
        "atr_tp2_mult":  trial.suggest_float("atr_tp2_mult", 1.5, 4.5),
        "buy_threshold": trial.suggest_int(  "buy_threshold", 52, 72),
    }
    trade_size_pct = trial.suggest_float("trade_size_pct", 0.04, 0.14)
    trailing_dist  = trial.suggest_float("trailing_dist",  0.005, 0.025)

    try:
        result = run_backtest(
            symbol=symbol,
            days=days,
            trade_size_pct=trade_size_pct,
            trailing_dist=trailing_dist,
            params=params,
            verbose=False,
        )

        if result["trades"] < 3:
            return -9999.0

        # Objectif : PnL % ajusté par le drawdown — pénalise les stratégies
        # très rentables mais instables.
        score = result["pnl_pct"] * (1.0 - result["max_drawdown"]) \
                * min(result["profit_factor"], 5.0)
        return score

    except Exception as e:
        print(f"[OPTUNA] Erreur trial {trial.number}: {e}")
        return -9999.0


def run_optimization(symbol: str = "SOL/EUR", days: int = 30,
                     n_trials: int = 50) -> dict:
    print(f"[OPTUNA] Optimisation {symbol} {days}j — {n_trials} trials")
    print(f"[OPTUNA] Étude persistée dans : {STUDY_DB}")

    study = optuna.create_study(
        direction="maximize",
        study_name=STUDY_NAME,
        storage=STUDY_DB,
        load_if_exists=True,
    )
    study.optimize(
        lambda t: objective(t, symbol, days),
        n_trials=n_trials,
        n_jobs=1,
        show_progress_bar=True,
    )

    _print_results(study)
    return study.best_params


def _print_results(study: optuna.Study):
    sep = "=" * 54
    best = study.best_params
    print(f"\n{sep}")
    print("  MEILLEURS PARAMÈTRES TROUVÉS")
    print(sep)
    for k, v in best.items():
        if isinstance(v, float):
            print(f"  {k:<22} = {v:.4f}")
        else:
            print(f"  {k:<22} = {v}")
    print(f"\n  Score objectif   : {study.best_value:.4f}")
    print(f"  Trials total     : {len(study.trials)}")
    print(sep)
    print("\n  Suggestion config.env :")
    print(f"  RSI_PERIOD={best.get('rsi_period', 14)}")
    print(f"  ATR_PERIOD={best.get('atr_period', 14)}")
    print(f"  BB_PERIOD={best.get('bb_period', 20)}")
    print(f"  ATR_SL_MULT={best.get('atr_sl_mult', 1.5):.2f}")
    print(f"  ATR_TP1_MULT={best.get('atr_tp1_mult', 1.0):.2f}")
    print(f"  ATR_TP2_MULT={best.get('atr_tp2_mult', 2.5):.2f}")
    print(f"  TRAILING_DISTANCE={best.get('trailing_dist', 0.012):.4f}")
    print(f"  TRADE_SIZE_PCT={best.get('trade_size_pct', 0.08):.3f}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Optimisation Optuna du bot")
    parser.add_argument("--symbol", default="SOL/EUR")
    parser.add_argument("--days",   type=int, default=30)
    parser.add_argument("--trials", type=int, default=50)
    parser.add_argument("--show",   action="store_true",
                        help="Afficher uniquement le meilleur résultat existant")
    args = parser.parse_args()

    if args.show:
        try:
            study = optuna.load_study(study_name=STUDY_NAME, storage=STUDY_DB)
            _print_results(study)
        except Exception as e:
            print(f"[ERREUR] Aucune étude trouvée : {e}")
    else:
        run_optimization(args.symbol, args.days, args.trials)
