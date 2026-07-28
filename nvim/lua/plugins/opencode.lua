local opencode_cmd = { "opencode", "--port" }
local snacks_terminal_opts = {
  win = {
    position = "right",
    width = 0.4,
    enter = false,
  },
}

return {
  "NickvanDyke/opencode.nvim",
  dependencies = {
    {
      "folke/snacks.nvim",
      opts = { input = {}, picker = {}, terminal = {} },
    },
  },
  keys = {
    {
      "<leader>oa",
      function()
        require("opencode").ask("@this: ")
      end,
      desc = "Ask OpenCode",
      mode = { "n", "x" },
    },
    {
      "<leader>os",
      function()
        require("opencode").select()
      end,
      desc = "Select OpenCode action",
    },
    {
      "<leader>ot",
      function()
        Snacks.terminal.toggle(opencode_cmd, snacks_terminal_opts)
      end,
      desc = "Toggle OpenCode",
      mode = { "n", "t" },
    },
    {
      "go",
      function()
        return require("opencode").operator("@this ")
      end,
      desc = "Append range to OpenCode",
      expr = true,
      mode = { "n", "x" },
    },
    {
      "goo",
      function()
        return require("opencode").operator("@this ") .. "_"
      end,
      desc = "Append line to OpenCode",
      expr = true,
      mode = "n",
    },
    {
      "<leader>ou",
      function()
        require("opencode").command("session.half.page.up")
      end,
      desc = "Scroll OpenCode up",
    },
    {
      "<leader>od",
      function()
        require("opencode").command("session.half.page.down")
      end,
      desc = "Scroll OpenCode down",
    },
  },
  config = function()
    vim.g.opencode_opts = {
      server = {
        start = function()
          Snacks.terminal.open(opencode_cmd, snacks_terminal_opts)
        end,
      },
    }
  end,
}
