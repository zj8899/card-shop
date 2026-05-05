use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MATrendStatus {
    pub period: usize,
    pub state: String, // "accelerating", "flattening", "decelerating"
    pub current_value: f64,
    pub prior_value: f64,
    pub bars_since_change: usize,
}

/// Compute simple moving average (SMA) for a series
pub fn compute_ma_series(prices: &[f64], period: usize) -> Vec<f64> {
    if period == 0 || prices.is_empty() || period > prices.len() {
        return vec![f64::NAN; prices.len()];
    }
    let mut result = vec![f64::NAN; prices.len()];
    let mut sum: f64 = prices[..period].iter().sum();
    result[period - 1] = sum / period as f64;
    for i in period..prices.len() {
        sum += prices[i] - prices[i - period];
        result[i] = sum / period as f64;
    }
    result
}

/// Compute all MAs for the given price series and periods
pub fn compute_all_mas(prices: &[f64], periods: &[usize]) -> HashMap<usize, Vec<f64>> {
    let mut results = HashMap::new();
    for &period in periods {
        results.insert(period, compute_ma_series(prices, period));
    }
    results
}

/// Detect trend state for a single MA line at current index
/// Compare current MA value with MA value from (period-1) bars ago
/// - period=34: look back 33 bars
/// - period=144: look back 143 bars
/// - period=233: look back 232 bars
pub fn detect_trend_state(ma_series: &[f64], period: usize, current_idx: usize) -> String {
    let lookback = period.saturating_sub(1);
    if current_idx < lookback {
        return "insufficient_data".to_string();
    }
    let current_val = ma_series[current_idx];
    let prior_val = ma_series[current_idx - lookback];

    if current_val.is_nan() || prior_val.is_nan() {
        return "insufficient_data".to_string();
    }

    let diff_pct = (current_val - prior_val) / prior_val.abs();

    if diff_pct > 0.002 {
        "accelerating".to_string()
    } else if diff_pct < -0.002 {
        "decelerating".to_string()
    } else {
        "flattening".to_string()
    }
}

/// Compute full trend detection for all periods at all positions
/// Returns a Vec of (index, period, state) tuples
pub fn detect_all_trends(
    prices: &[f64],
    periods: &[usize],
) -> Vec<MATrendStatus> {
    let mas = compute_all_mas(prices, periods);
    let mut results = Vec::new();

    for (&period, ma_series) in &mas {
        for i in (period - 1)..ma_series.len() {
            if !ma_series[i].is_nan() {
                let lookback = period.saturating_sub(1);
                let state = detect_trend_state(ma_series, period, i);
                let prior_val = if i >= lookback {
                    ma_series[i - lookback]
                } else {
                    f64::NAN
                };
                results.push(MATrendStatus {
                    period,
                    state,
                    current_value: ma_series[i],
                    prior_value: prior_val,
                    bars_since_change: 0,
                });
            }
        }
    }
    results
}

/// Compute 4-bar arithmetic mean of MA values for smoothed status
pub fn compute_4bar_avg(ma_series: &[f64]) -> Vec<f64> {
    if ma_series.len() < 4 {
        return vec![f64::NAN; ma_series.len()];
    }
    let mut result = vec![f64::NAN; ma_series.len()];
    for i in 3..ma_series.len() {
        let valid_count = (i - 3..=i)
            .filter(|&j| !ma_series[j].is_nan())
            .count();
        if valid_count == 4 {
            let sum: f64 = (i - 3..=i).map(|j| ma_series[j]).sum();
            result[i] = sum / 4.0;
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_compute_ma() {
        let prices: Vec<f64> = (1..=10).map(|x| x as f64).collect();
        let ma3 = compute_ma_series(&prices, 3);
        assert!(ma3[2].is_finite());
        assert!((ma3[2] - 2.0).abs() < 0.01); // avg of 1,2,3
        assert!((ma3[9] - 9.0).abs() < 0.01); // avg of 8,9,10
    }

    #[test]
    fn test_trend_detection() {
        // Create a gradually increasing MA series
        let ma: Vec<f64> = (0..200).map(|x| x as f64 * 0.1 + 10.0).collect();
        let state = detect_trend_state(&ma, 34, 100);
        assert_eq!(state, "accelerating");

        // Flat MA
        let ma_flat = vec![10.0; 200];
        let state = detect_trend_state(&ma_flat, 34, 100);
        assert_eq!(state, "flattening");
    }
}
