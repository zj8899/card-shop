use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FibLevel {
    pub ratio: f64,
    pub price: f64,
    pub label: String,
}

/// Compute Fibonacci retracement levels
/// From a swing high to swing low (or vice versa)
pub fn fib_retracement(high: f64, low: f64) -> Vec<FibLevel> {
    let diff = high - low;
    let ratios = vec![
        (0.0, "0.0 (起点)"),
        (0.236, "0.236"),
        (0.382, "0.382"),
        (0.5, "0.5"),
        (0.618, "0.618"),
        (0.786, "0.786"),
        (1.0, "1.0 (终点)"),
    ];

    ratios
        .into_iter()
        .map(|(ratio, label)| FibLevel {
            ratio,
            price: high - diff * ratio,
            label: label.to_string(),
        })
        .collect()
}

/// Compute Fibonacci extension levels (for projecting beyond the retracement)
pub fn fib_extension(high: f64, low: f64, retrace: f64) -> Vec<FibLevel> {
    let diff = high - low;
    let ratios = vec![
        (1.0, "1.0"),
        (1.272, "1.272"),
        (1.618, "1.618"),
        (2.0, "2.0"),
        (2.618, "2.618"),
    ];

    ratios
        .into_iter()
        .map(|(ratio, label)| FibLevel {
            ratio,
            price: retrace + diff * ratio,
            label: label.to_string(),
        })
        .collect()
}

/// Find local swing highs and lows from price series
/// Returns (high_indices, high_prices, low_indices, low_prices)
pub fn find_swings(prices: &[f64], window: usize) -> (Vec<usize>, Vec<f64>, Vec<usize>, Vec<f64>) {
    let mut high_indices = Vec::new();
    let mut high_prices = Vec::new();
    let mut low_indices = Vec::new();
    let mut low_prices = Vec::new();

    for i in window..prices.len() - window {
        let slice = &prices[i - window..=i + window];
        let center = prices[i];

        let is_high = slice.iter().all(|&p| p <= center);
        let is_low = slice.iter().all(|&p| p >= center);

        if is_high {
            high_indices.push(i);
            high_prices.push(center);
        }
        if is_low {
            low_indices.push(i);
            low_prices.push(center);
        }
    }
    (high_indices, high_prices, low_indices, low_prices)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fib_retracement() {
        let levels = fib_retracement(100.0, 50.0);
        assert_eq!(levels.len(), 7);
        let level_618 = &levels[4];
        assert!((level_618.ratio - 0.618).abs() < 0.001);
        let expected_price = 100.0 - 50.0 * 0.618;
        assert!((level_618.price - expected_price).abs() < 0.01);
    }

    #[test]
    fn test_find_swings() {
        let prices = vec![
            5.0, 6.0, 7.0, 6.0, 5.0, 4.0, 5.0, 6.0, 7.0, 8.0, 7.0, 6.0,
        ];
        let (highs, _, lows, _) = find_swings(&prices, 2);
        assert!(highs.contains(&9)); // index 9 = 8.0 is a high
        assert!(lows.contains(&5)); // index 5 = 4.0 is a low
    }
}
