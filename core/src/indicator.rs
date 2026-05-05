use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KDJResult {
    pub k: Vec<f64>,
    pub d: Vec<f64>,
    pub j: Vec<f64>,
}

/// Compute KDJ indicator
/// Standard KDJ:
/// - RSV(n) = (C - Ln) / (Hn - Ln) * 100
/// - K = 2/3 * prev_K + 1/3 * RSV
/// - D = 2/3 * prev_D + 1/3 * K
/// - J = 3 * K - 2 * D
pub fn compute_kdj(high: &[f64], low: &[f64], close: &[f64], n: usize) -> KDJResult {
    let len = high.len();
    let mut k_values = vec![50.0; len];
    let mut d_values = vec![50.0; len];
    let mut j_values = vec![50.0; len];

    for i in n - 1..len {
        let h_max = high[i - n + 1..=i]
            .iter()
            .cloned()
            .fold(f64::NEG_INFINITY, f64::max);
        let l_min = low[i - n + 1..=i]
            .iter()
            .cloned()
            .fold(f64::INFINITY, f64::min);

        let rsv = if (h_max - l_min).abs() < 1e-10 {
            50.0
        } else {
            ((close[i] - l_min) / (h_max - l_min)) * 100.0
        };

        let prev_k = if i > 0 { k_values[i - 1] } else { 50.0 };
        let prev_d = if i > 0 { d_values[i - 1] } else { 50.0 };

        k_values[i] = (2.0 / 3.0) * prev_k + (1.0 / 3.0) * rsv;
        d_values[i] = (2.0 / 3.0) * prev_d + (1.0 / 3.0) * k_values[i];
        j_values[i] = 3.0 * k_values[i] - 2.0 * d_values[i];
    }

    KDJResult {
        k: k_values,
        d: d_values,
        j: j_values,
    }
}

/// Compute SKDJ (Slow KDJ) - smoothed version
/// SKDJ uses a second smoothing pass on K and D
pub fn compute_skdj(high: &[f64], low: &[f64], close: &[f64], n: usize, m: usize) -> KDJResult {
    let kdj = compute_kdj(high, low, close, n);
    let len = kdj.k.len();
    let mut sk = vec![50.0; len];
    let mut sd = vec![50.0; len];
    let mut sj = vec![50.0; len];

    for i in m..len {
        let prev_sk = if i >= m { sk[i - 1] } else { 50.0 };
        let prev_sd = if i >= m { sd[i - 1] } else { 50.0 };

        sk[i] = (2.0 / 3.0) * prev_sk + (1.0 / 3.0) * kdj.k[i];
        sd[i] = (2.0 / 3.0) * prev_sd + (1.0 / 3.0) * sk[i];
        sj[i] = 3.0 * sk[i] - 2.0 * sd[i];
    }

    KDJResult {
        k: sk,
        d: sd,
        j: sj,
    }
}

/// Detect KDJ bullish divergence: price makes lower low, but K makes higher low
pub fn detect_kdj_divergence(
    prices: &[f64],
    k_values: &[f64],
    window: usize,
) -> Vec<(usize, String)> {
    let mut divergences = Vec::new();
    let len = prices.len().min(k_values.len());

    for i in window * 2..len {
        let price_slice = &prices[i - window * 2..=i];
        let k_slice = &k_values[i - window * 2..=i];

        // Find local price min and K min in the window
        let mut price_min_idx = 0;
        let mut price_min = f64::INFINITY;
        for j in 0..window {
            if price_slice[j] < price_min {
                price_min = price_slice[j];
                price_min_idx = j;
            }
        }

        let mut recent_price_min = f64::INFINITY;
        let mut recent_k_at_price_min = 0.0;
        for j in window..window * 2 {
            if price_slice[j] < recent_price_min {
                recent_price_min = price_slice[j];
                recent_k_at_price_min = k_slice[j];
            }
        }

        let old_k_at_price_min = k_slice[price_min_idx];

        // Bullish divergence: lower low in price, higher low in K
        if recent_price_min < price_min && recent_k_at_price_min > old_k_at_price_min {
            divergences.push((i, "bullish".to_string()));
        }
    }
    divergences
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_kdj_basic() {
        let high = vec![10.0; 20];
        let low = vec![5.0; 20];
        let close: Vec<f64> = (0..20).map(|i| 5.0 + i as f64 * 0.25).collect();
        let kdj = compute_kdj(&high, &low, &close, 9);
        assert!(kdj.k[18].is_finite());
        assert!(kdj.d[18].is_finite());
        assert!(kdj.j[18].is_finite());
    }
}
