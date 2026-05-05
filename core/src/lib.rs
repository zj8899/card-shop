mod fibonacci;
mod indicator;
mod ma;
mod signal;

use pyo3::prelude::*;
use serde_json;

/// Compute MA series for given prices and period
#[pyfunction]
fn compute_ma(prices: Vec<f64>, period: usize) -> PyResult<Vec<f64>> {
    Ok(ma::compute_ma_series(&prices, period))
}

/// Compute all MAs (34, 144, 233) and their trend states
/// Returns JSON string with all computed values
#[pyfunction]
fn analyze_trends(
    ohlcv_json: &str,
    timeframe: &str,
) -> PyResult<String> {
    #[derive(serde::Deserialize)]
    struct OHLCV {
        open: Vec<f64>,
        high: Vec<f64>,
        low: Vec<f64>,
        close: Vec<f64>,
        volume: Vec<f64>,
    }

    let data: OHLCV = serde_json::from_str(ohlcv_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON: {}", e)))?;

    let close = &data.close;
    let periods = match timeframe {
        "daily" | "day" => vec![5_usize, 13, 21, 34, 55, 144, 233, 623],
        _ => vec![34_usize, 144, 233],
    };

    let mas = ma::compute_all_mas(close, &periods);

    // Collect trend states efficiently
    let mut trend_data = Vec::new();
    for (&period, ma_series) in &mas {
        let valid_start = period.saturating_sub(1);
        for i in valid_start..ma_series.len() {
            let state = ma::detect_trend_state(ma_series, period, i);
            trend_data.push(serde_json::json!({
                "index": i,
                "period": period,
                "state": state,
                "ma_value": ma_series[i],
            }));
        }
    }

    // Compute KDJ
    let kdj = indicator::compute_kdj(&data.high, &data.low, close, 9);
    let skdj = indicator::compute_skdj(&data.high, &data.low, close, 9, 3);

    // Compute 4-bar averages for each MA
    let mut ma_4bar_avgs = serde_json::Map::new();
    for (&period, ma_series) in &mas {
        let avg = ma::compute_4bar_avg(ma_series);
        let avg_clean: Vec<Option<f64>> = avg
            .into_iter()
            .map(|v| if v.is_nan() { None } else { Some(v) })
            .collect();
        ma_4bar_avgs.insert(period.to_string(), serde_json::to_value(&avg_clean).unwrap());
    }

    let mut mas_map = serde_json::Map::new();
    for (p, vals) in &mas {
        let clean: Vec<Option<f64>> = vals.iter().map(|v| if v.is_nan() { None } else { Some(*v) }).collect();
        mas_map.insert(p.to_string(), serde_json::to_value(&clean).unwrap());
    }

    let result = serde_json::json!({
        "mas": mas_map,
        "trends": trend_data,
        "kdj": {
            "k": kdj.k.iter().map(|v| if v.is_nan() { None } else { Some(*v) }).collect::<Vec<_>>(),
            "d": kdj.d.iter().map(|v| if v.is_nan() { None } else { Some(*v) }).collect::<Vec<_>>(),
            "j": kdj.j.iter().map(|v| if v.is_nan() { None } else { Some(*v) }).collect::<Vec<_>>(),
        },
        "skdj": {
            "k": skdj.k.iter().map(|v| if v.is_nan() { None } else { Some(*v) }).collect::<Vec<_>>(),
            "d": skdj.d.iter().map(|v| if v.is_nan() { None } else { Some(*v) }).collect::<Vec<_>>(),
            "j": skdj.j.iter().map(|v| if v.is_nan() { None } else { Some(*v) }).collect::<Vec<_>>(),
        },
        "ma_4bar_avg": ma_4bar_avgs,
        "timeframe": timeframe,
    });

    Ok(serde_json::to_string(&result).unwrap())
}

/// Fibonacci retracement calculation
#[pyfunction]
fn fib_retrace(high: f64, low: f64) -> PyResult<String> {
    let levels = fibonacci::fib_retracement(high, low);
    serde_json::to_string(&levels)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Serialize error: {}", e)))
}

/// Fibonacci extension calculation
#[pyfunction]
fn fib_extend(high: f64, low: f64, retrace: f64) -> PyResult<String> {
    let levels = fibonacci::fib_extension(high, low, retrace);
    serde_json::to_string(&levels)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Serialize error: {}", e)))
}

/// Find swing highs and lows
#[pyfunction]
fn find_swing_points(prices: Vec<f64>, window: usize) -> PyResult<String> {
    let (hi_idx, hi_prices, lo_idx, lo_prices) = fibonacci::find_swings(&prices, window);
    let result = serde_json::json!({
        "highs": {"indices": hi_idx, "prices": hi_prices},
        "lows": {"indices": lo_idx, "prices": lo_prices},
    });
    Ok(serde_json::to_string(&result).unwrap())
}

/// Detect bullish/bearish KDJ divergence
#[pyfunction]
fn detect_divergence(prices: Vec<f64>, k_values: Vec<f64>, window: usize) -> PyResult<String> {
    let divs = indicator::detect_kdj_divergence(&prices, &k_values, window);
    serde_json::to_string(&divs)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Serialize error: {}", e)))
}

/// Python module definition
#[pymodule]
fn sancai_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_ma, m)?)?;
    m.add_function(wrap_pyfunction!(analyze_trends, m)?)?;
    m.add_function(wrap_pyfunction!(fib_retrace, m)?)?;
    m.add_function(wrap_pyfunction!(fib_extend, m)?)?;
    m.add_function(wrap_pyfunction!(find_swing_points, m)?)?;
    m.add_function(wrap_pyfunction!(detect_divergence, m)?)?;
    Ok(())
}
