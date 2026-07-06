Run our java version of FlakyGuard using the below command:

bash run_all_flakyguard.sh > fg_run.log

It automatically takes `test_config.csv` as input. Since we use ReproFlake as our dataset, the input file is the same structure as the dataset's original test_config.csv (https://anonymous.4open.science/r/ReproFlake-C9E6/test_config.csv).

fg_run.log would contain the names of individual log files for each test.
If FlakyGuard is able to create a successful patch, it will be in the `succesful_patches` folder.
