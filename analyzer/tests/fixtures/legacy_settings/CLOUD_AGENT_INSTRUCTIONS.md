# Legacy Settings Fixtures - Cloud Agent Task

## Goal
Generate settings.json files from past Kestrel versions to test settings forward/backward compatibility.

## Instructions

### 1. List all release tags
```bash
git log --tags --simplify-by-decoration --pretty="format:%d %H" | grep "tag:" | head -20
```

### 2. For each tag, identify available settings keys

Checkout each tag and inspect the settings system:
```bash
git checkout <tag>
cat analyzer/settings_utils.py | grep -A 200 "DEFAULT_SETTINGS = \|_DEFAULTS = \|^def load_persisted_settings"
```

Document which keys existed at that version.

### 3. Generate synthetic settings.json files

For each unique version with a different set of keys, create: `settings_v{tag_name}.json`

**Example:** If version 1.8.0 had keys `[editor, detection_threshold, scene_time_threshold]`:
- File: `settings_v1.8.0.json`
- Contents: JSON with all keys present at that version, filled with reasonable values
- **Do NOT include** keys that were added in later versions
- Example structure:
  ```json
  {
    "editor": "/Applications/Lightroom.app",
    "detection_threshold": 0.3,
    "scene_time_threshold": 1200,
    "rating_profile": "balanced"
  }
  ```

### 4. Generate edge case variants

For at least one version, create two additional variants:
- `settings_v{version}_minimal.json` — only required keys, defaults for optional keys
- `settings_v{version}_maximal.json` — all known keys at that version, various non-default values

### 5. Document key evolution

Create a `KEY_EVOLUTION.md` file documenting:
- Which version added which keys
- Which keys were removed
- Which keys changed types or default values
- The sequence of settings schema evolution

## Example Output Structure

```
legacy_settings/
├── KEY_EVOLUTION.md
├── settings_v1.8.0.json
├── settings_v1.8.0_minimal.json
├── settings_v1.8.0_maximal.json
├── settings_v1.9.0.json
├── settings_v2.0.0.json
└── CLOUD_AGENT_INSTRUCTIONS.md      # This file
```

## Important Notes

- Focus on versions with KEY CHANGES, not every minor release
- The test suite expects files named exactly `settings_v{version}.json`
- When testing, the code must handle old keys gracefully (via `_passthrough_setting_value`)
- Monotonic counters (like `kestrel_impact_total_files`) should be high in old versions to test the guard logic
- Include at least one version that had different default values for a key (e.g., different threshold defaults)

## Testing Strategy

The test suite will:
1. Load each legacy settings file via `load_persisted_settings()`
2. Verify all old keys are still present (no keys dropped)
3. Verify values are unchanged for stable keys
4. Verify monotonic counters never go backward
5. Verify unknown keys are preserved for forward compatibility
