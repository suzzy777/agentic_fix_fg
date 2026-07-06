First, set the LLM provider, we used Claude as our LLM using:

`export FLAKYGUARD_LLM_PROVIDER=anthropic`

Then set the API key for the given LLM:

`export ANTHROPIC_API_KEY="<insert API key here>"`

Please make sure Docker is installed and running on your machine.

Please clone the folder, all files are required to be in the root for the tool to run. Run our java version of FlakyGuard using the below command:

`bash run_all_flakyguard.sh > fg_run.log`

It automatically takes `test_config.csv` as input. Since we use ReproFlake as our dataset, the input file is the same structure as the dataset's original test_config.csv (https://anonymous.4open.science/r/ReproFlake-C9E6/test_config.csv).

`fg_run.log` would contain the names of individual log files for each test.
If FlakyGuard is able to create a successful patch, it will be in the `succesful_patches` folder.


