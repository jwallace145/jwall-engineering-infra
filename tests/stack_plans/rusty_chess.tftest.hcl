# The live bot's inputs: every part of the stack is on.
mock_provider "aws" {
  source = "./tests/mocks"
}

run "plans_a_bot_with_its_tasks_and_dataset" {
  command = plan

  assert {
    condition     = var.bot_enabled && length(module.image) == 1 && length(module.bot) == 1
    error_message = "a bot project has an image repository and an instance"
  }

  assert {
    condition     = length(module.records) == 1
    error_message = "a bot project has a records bucket"
  }

  assert {
    condition     = length(module.network) == 1 && length(module.tasks) == 1 && length(module.task_results) == 1
    error_message = "rusty-chess has a network, a task runner and a results bucket"
  }

  assert {
    condition     = keys(module.datasets) == ["nnue"]
    error_message = "rusty-chess has the nnue dataset"
  }

  assert {
    condition     = toset(keys(module.task_profiles)) == toset(["datagen", "gpu", "match", "relabel", "sprt"])
    error_message = "rusty-chess has one launch template per task profile"
  }
}
