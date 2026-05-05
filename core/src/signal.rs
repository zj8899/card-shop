use serde::{Deserialize, Serialize};

use crate::indicator::KDJResult;
use crate::ma;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Signal {
    pub index: usize,
    pub signal_type: String, // "BUY_POINT_1", "BUY_POINT_2", "ADD_POSITION", "REDUCE", "CLEAR"
    pub price: f64,
    pub reason: String,
}

/// Check Buy Point 1 conditions (30min timeframe core):
/// - Price breaks below 34 MA
/// - Price makes new low below 34 MA
/// - 144 MA decelerates, then price breaks below 233 MA
/// - Consecutive break of 233/144/34 MA + break previous low + KDJ divergence
pub fn check_buy_point_1(
    idx: usize,
    close: &[f64],
    low: &[f64],
    ma_34: &[f64],
    ma_144: &[f64],
    ma_233: &[f64],
    kdj_result: &KDJResult,
    prev_lows: &[f64],
) -> Option<Signal> {
    if idx < 233 {
        return None;
    }

    let price = close[idx];
    let current_low = low[idx];

    // Condition: Price breaks below 34 MA at current bar
    let below_ma34 = price < ma_34[idx];

    // Condition: Price makes a new low below 34 MA
    let new_low_below_ma34 = current_low < prev_lows.iter().cloned().fold(f64::INFINITY, f64::min);

    // Condition: 144 MA is decelerating
    let ma144_state = ma::detect_trend_state(ma_144, 144, idx);
    let ma144_decelerating = ma144_state == "decelerating";

    // Condition: Price is below 233 MA
    let below_ma233 = price < ma_233[idx];

    // Check for KDJ divergence (bullish)
    let kdj_diverged = check_kdj_divergence_at(kdj_result, idx);

    // Core buy logic: below 34MA + new low + 144 decelerating + below 233MA + KDJ divergence
    if below_ma34 && new_low_below_ma34 && ma144_decelerating && below_ma233 && kdj_diverged {
        return Some(Signal {
            index: idx,
            signal_type: "BUY_POINT_1".to_string(),
            price,
            reason: format!(
                "跌破34MA({:.2})+新低+144MA减速+跌破233MA({:.2})+KDJ底背离",
                ma_34[idx], ma_233[idx]
            ),
        });
    }

    None
}

/// Check Buy Point 2: Price reclaims 34 MA or breaks above 144/233 MA after Buy Point 1
pub fn check_buy_point_2(
    idx: usize,
    close: &[f64],
    ma_34: &[f64],
    ma_144: &[f64],
    ma_233: &[f64],
    buy_point_1_idx: Option<usize>,
) -> Option<Signal> {
    if idx < 233 {
        return None;
    }

    let price = close[idx];

    match buy_point_1_idx {
        Some(bp1_idx) => {
            // Must be within 3 days (for 30min, ~48 bars of 30min = 3 trading days)
            let bars_since_bp1 = idx - bp1_idx;
            if bars_since_bp1 > 48 {
                return None; // 3 days passed, abandon
            }

            // Confirmation: price reclaims 34 MA
            if price > ma_34[idx] {
                return Some(Signal {
                    index: idx,
                    signal_type: "BUY_POINT_2".to_string(),
                    price,
                    reason: format!("收复34MA确认({:.2}), BP1后{}K线", ma_34[idx], bars_since_bp1),
                });
            }
        }
        None => {
            // Standalone BP2: price breaks above both 144 and 233 MA
            if price > ma_144[idx] && price > ma_233[idx] {
                let above_ma34 = price > ma_34[idx];
                if above_ma34 {
                    return Some(Signal {
                        index: idx,
                        signal_type: "BUY_POINT_2".to_string(),
                        price,
                        reason: format!(
                            "突破144MA({:.2})+233MA({:.2})",
                            ma_144[idx], ma_233[idx]
                        ),
                    });
                }
            }
        }
    }

    None
}

/// Check risk management signals
pub fn check_risk_signals(
    idx: usize,
    close: &[f64],
    ma_34: &[f64],
    ma_144: &[f64],
    ma_233: &[f64],
    timeframe: &str,
) -> Vec<Signal> {
    let mut signals = Vec::new();

    if idx < 233 {
        return signals;
    }

    let price = close[idx];

    match timeframe {
        "1min" => {
            // 1min: break 34 or 233 MA -> reduce
            if price < ma_34[idx] || price < ma_233[idx] {
                signals.push(Signal {
                    index: idx,
                    signal_type: "REDUCE".to_string(),
                    price,
                    reason: "1分钟跌破34/233均线→减仓".to_string(),
                });
            }
        }
        "5min" => {
            // 5min: break 233 MA -> clear
            if price < ma_233[idx] {
                signals.push(Signal {
                    index: idx,
                    signal_type: "CLEAR".to_string(),
                    price,
                    reason: "5分钟跌破233均线→清仓".to_string(),
                });
            }
        }
        "30min" => {
            // 30min: break 144 MA -> reduce; break 144 and 233 -> clear
            if price < ma_144[idx] && price < ma_233[idx] {
                signals.push(Signal {
                    index: idx,
                    signal_type: "CLEAR".to_string(),
                    price,
                    reason: "30分钟跌破144+233均线→清仓".to_string(),
                });
            } else if price < ma_144[idx] {
                signals.push(Signal {
                    index: idx,
                    signal_type: "REDUCE".to_string(),
                    price,
                    reason: "30分钟跌破144均线→减仓".to_string(),
                });
            }
        }
        "60min" => {
            // 60min: break 34 MA for 3 days -> reduce
            if price < ma_34[idx] {
                signals.push(Signal {
                    index: idx,
                    signal_type: "REDUCE".to_string(),
                    price,
                    reason: "60分钟跌破34均线→减仓".to_string(),
                });
            }
        }
        _ => {}
    }

    signals
}

/// Check if there's a bullish KDJ divergence at current index
fn check_kdj_divergence_at(kdj: &KDJResult, idx: usize) -> bool {
    if idx < 20 {
        return false;
    }

    let window = 9;
    let start = idx - window * 2;

    // Find first low in window range
    let mut first_low_k = f64::INFINITY;
    for j in start..start + window {
        if j < kdj.k.len() && kdj.k[j] < first_low_k {
            first_low_k = kdj.k[j];
        }
    }

    // Find second low in recent window
    let mut second_low_k = f64::INFINITY;
    for j in start + window..=idx {
        if j < kdj.k.len() && kdj.k[j] < second_low_k {
            second_low_k = kdj.k[j];
        }
    }

    // Bullish divergence: second low in K is higher than first
    second_low_k > first_low_k && kdj.k[idx] < 30.0 // Oversold zone
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_buy_point_1_none_when_above_ma() {
        let close: Vec<f64> = (0..300).map(|i| 10.0 + i as f64 * 0.01).collect();
        let low = close.clone();
        let ma34 = vec![10.5; 300];
        let ma144 = vec![10.5; 300];
        let ma233 = vec![10.5; 300];
        let kdj = KDJResult {
            k: vec![50.0; 300],
            d: vec![50.0; 300],
            j: vec![50.0; 300],
        };
        let prev_lows = vec![9.0; 300];
        let sig = check_buy_point_1(250, &close, &low, &ma34, &ma144, &ma233, &kdj, &prev_lows);
        assert!(sig.is_none()); // Price above all MAs
    }
}
