from setuptools import setup, find_packages

setup(
    name="smart-bandwidth-optimizer",
    version="1.0.0",
    description=(
        "A system that prioritizes important traffic, compresses data, "
        "and drops unnecessary packets to optimize bandwidth usage."
    ),
    packages=find_packages(exclude=["tests*"]),
    python_requires=">=3.9",
    install_requires=[],
    extras_require={
        "dev": ["pytest>=7.0"],
    },
    entry_points={
        "console_scripts": [
            "bandwidth-optimizer=main:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
)
