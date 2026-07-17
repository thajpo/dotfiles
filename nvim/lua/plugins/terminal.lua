return {
  -- Configure Snacks.nvim terminal (included with LazyVim)
  {
    "folke/snacks.nvim",
    opts = {
      input = {},
      picker = {
        sources = {
          explorer = {
            ignored = true,
          },
          files = {
            ignored = true,
          },
        },
      },
      terminal = {
        win = {
          style = "float",
          width = 0.85,
          height = 0.8,
          border = "rounded",
        },
      },
    },
    keys = {
      -- Floating terminal with <leader>ft
      {
        "<leader>ft",
        function()
          Snacks.terminal.toggle()
        end,
        desc = "Floating Terminal",
      },
      -- Open a second terminal with <leader>fT (useful for running tests while keeping main terminal)
      {
        "<leader>fT",
        function()
          Snacks.terminal.toggle(nil, { cwd = vim.fn.getcwd(), win = { style = "float" } })
        end,
        desc = "Floating Terminal (cwd)",
      },
    },
  },
}
