# Configuration additions for client requirements

# ============================================================================
# PYRAMID/SCALING CONFIGURATION (Stage 4.5)
# ============================================================================

# Enable pyramid trading (add to winners)
ENABLE_PYRAMID = True

# Profit threshold for first scale (%)
PYRAMID_PROFIT_THRESHOLD_1 = 3.0

# Profit threshold for second scale (%)
PYRAMID_PROFIT_THRESHOLD_2 = 6.0

# Size to add at first threshold (% of original position)
PYRAMID_ADD_SIZE_1 = 0.5  # 50% more

# Size to add at second threshold (% of original position)
PYRAMID_ADD_SIZE_2 = 0.25  # 25% more

# Maximum total position size (multiplier of original)
PYRAMID_MAX_SIZE_MULTIPLIER = 2.0  # Never exceed 2x original

# Poll interval for checking positions (seconds)
PYRAMID_POLL_INTERVAL_SECONDS = 30

