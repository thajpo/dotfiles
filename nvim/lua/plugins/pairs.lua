return {
  {
    "nvim-mini/mini.pairs",
    opts = function(_, opts)
      opts.mappings = opts.mappings or {}
      -- Backticks are meaningful in Markdown and template strings. Insert them
      -- literally instead of auto-pairing or expanding Markdown code fences.
      opts.mappings["`"] = false
    end,
  },
}
