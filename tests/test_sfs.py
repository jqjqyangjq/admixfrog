
def test_basic(script_runner):
    """test case 1"""
    cmd = 'admixslug --infile data/oase_chr9.in.xz --ref data/ref_A1240k.csv.xz '
    cmd += ' --out res/test_sfs --seed 13 --force-infile --states AFR NEA -b 100000 -P'
    args = cmd.split()
    print(args)
    ret = script_runner.run(*args, cwd='tests')
    print(ret.stdout)
    print(ret.stderr)
    assert ret.success

