# python script to write all the added parameters to a pbs script, which can then be executed on athena0

import os
import argparse

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--outfile", type=str, default="slurm")
    parser.add_argument(
        "-j",
        "--jobname",
        type=str,
        dest="jobname",
        help="run job with name= jobname",
        default=None,
    )
    parser.add_argument(
        "-e", "--email", type=str, dest="email", help="send email here", default=None
    )
    parser.add_argument(
        "-t",
        "--time",
        type=str,
        dest="time",
        help="estimated time for job to run",
        default="18:00:00",
    )
    parser.add_argument(
        "-n",
        "--ntasks",
        type=int,
        dest="ntasks",
        help="number of tasks (cpus to use)",
        default=1,
    )
    parser.add_argument("-d", "--dir", type=str, dest="dir", default=None)
    parser.add_argument(
        "-c",
        "--command",
        type=str,
        dest="command",
        help="command(s) to run",
        default=None,
    )
    parser.add_argument(
        "-f",
        "--commandfile",
        type=str,
        dest="commandfile",
        help="use content of commandfile as executable",
        default=None,
    )
    args = parser.parse_args()

    if args.commandfile is not None and args.command is not None:
        args.command = None
        print("Using --commandfile instead of --command")

    if args.dir is None:
        args.dir = os.getcwd()

    with open(args.outfile, "w") as outfile:

        outfile.write("#!/bin/bash\n")
        outfile.write("\n")
        outfile.write("#SBATCH --account=rubin:developers\n")
        outfile.write("#SBATCH --partition=milano\n")

        if args.jobname is None:
            if args.command is not None:
                jobname = args.command.replace(" ", "-")
            if args.commandfile is not None:
                jobname = args.commandfile
            slurm_outputs = "output"
        else:
            jobname = args.jobname
            slurm_outputs = jobname
        outfile.write(f"#SBATCH --job-name={jobname}\n")
        outfile.write(f"#SBATCH --output={slurm_outputs}-out-%j.txt\n")
        outfile.write(f"#SBATCH --error={slurm_outputs}-err-%j.txt\n")

        if args.email is not None:
            outfile.write("#SBATCH --mail-type=ALL\n")
            outfile.write(f"#SBATCH --mail-user={args.email}\n")

        outfile.write(f"#SBATCH --time={args.time}\n")
        # The number of nodes/tasks should be job dependent
        outfile.write("#SBATCH --nodes=1\n")
        outfile.write(f"#SBATCH --ntasks={args.ntasks}\n")
        mem = args.ntasks * 20
        outfile.write(f"#SBATCH --mem={mem}g\n")
        outfile.write(f"#SBATCH --cpus-per-task=1\n")
        outfile.write(f"#SBATCH --chdir={args.dir}\n")
        outfile.write("\n")

        # Have the slurm file tell us a bit about where and when it's running
        outfile.write("echo Job starting at `date`\n")
        outfile.write("echo Job running on `hostname`\n")
        outfile.write("\n")

        # Assume we're running in rubin-sim conda
        outfile.write("source ~/.bashrc\n")
        outfile.write("conda activate rubin-sim\n")
        outfile.write("export OPENBLAS_NUM_THREADS=1\n")
        outfile.write("\n")

        # Now copy in command or commandfile
        if args.command is not None:
            outfile.write(args.command)
            outfile.write("\n")

        else:
            with open(args.commandfile, "r") as f:
                lines = f.read()
            outfile.write(lines)

        outfile.write("echo Job ending at `date`\n")
