return {
  {
    "milanglacier/minuet-ai.nvim",
    cmd = "Minuet",
    event = "InsertEnter",
    opts = {
      provider = "openai_compatible",
      request_timeout = 2.5,
      throttle = 1500,
      debounce = 600,
      virtualtext = {
        -- Keep requests explicit. Use :Minuet virtualtext enable to opt a
        -- buffer into automatic suggestions when desired.
        auto_trigger_ft = {},
        keymap = {
          accept = "<A-A>",
          accept_line = "<A-a>",
          accept_n_lines = "<A-z>",
          prev = "<A-[>",
          next = "<A-]>",
          dismiss = "<A-e>",
        },
      },
      provider_options = {
        openai_compatible = {
          api_key = "OPENROUTER_API_KEY",
          end_point = "https://openrouter.ai/api/v1/chat/completions",
          model = "deepseek/deepseek-v4-flash",
          name = "Openrouter",
          optional = {
            max_tokens = 56,
            top_p = 0.9,
            provider = {
              sort = "throughput",
            },
            reasoning_effort = "none",
          },
        },
      },
    },
  },
  {
    "nvim-lualine/lualine.nvim",
    optional = true,
    opts = function(_, opts)
      table.insert(opts.sections.lualine_x, 1, require("minuet.lualine"))
    end,
  },
}
