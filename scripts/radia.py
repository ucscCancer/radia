#!/usr/bin/env python

import sys
import time
import re
import os
import subprocess
import datetime
import logging
#from argparse import ArgumentParser
from optparse import OptionParser
from itertools import izip
import radiaUtil
import collections
import gzip


'''
'    RNA and DNA Integrated Analysis (RADIA) identifies RNA and DNA variants in NGS data.
'    Copyright (C) 2010-2015  Amie Radenbaugh
'
'    This program is free software: you can redistribute it and/or modify
'    it under the terms of the GNU Affero General Public License as
'    published by the Free Software Foundation, either version 3 of the
'    License, or (at your option) any later version.
'
'    This program is distributed in the hope that it will be useful,
'    but WITHOUT ANY WARRANTY; without even the implied warranty of
'    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
'    GNU Affero General Public License for more details.
'
'    You should have received a copy of the GNU Affero General Public License
'    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'
'    This program identifies RNA and DNA variants in BAM files.  The program is designed
'    to take in 4 BAM files:  DNA Normal, RNA Normal, DNA Tumor, and RNA Tumor.  For the 
'    normal DNA, the program outputs any differences compared to the reference which could 
'    be potential Germline mutations.  For the normal RNA, the program outputs any differences 
'    compared to the reference and the normal DNA which could be potential normal RNA-Editing
'    events.  For the tumor DNA, the program outputs any difference compared to the reference, 
'    normal DNA and normal RNA which could be potential Somatic mutations.  For the tumor
'    RNA, the program outputs any difference compared to the reference, normal DNA, normal 
'    RNA and tumor DNA which could be potential RNA-editing events.
'
'    The program is designed for 4 BAM files, but the user can also specify just two or three.
'    The program will report RNA and DNA variants.
'
'''

# this regular expression is used to remove insertions and deletions from raw reads
# a read could look like:  "T$TT+3AGGGT+2AG+2AG.-2AGGG..-1A"
# insertions start with a "+", deletions with a "-"
# in theory, there could be multiple digits
i_numOfIndelsRegEx = re.compile("[+-]{1}(\\d+)")

# this regular expression will match any number of valid cDNA strings
i_cDNARegEx = re.compile("[ACGTNacgtn]+")

# this regular expression will match full TCGA sample Ids, e.g. TCGA-AG-A016-01A-01R or TCGA-37-4133-10A-01D
i_tcgaNameRegEx = re.compile("TCGA-(\\w){2}-(\\w){4}-(\\w){3}-(\\w){3}")


def get_read_fileHandler(aFilename):
    '''
    ' Open aFilename for reading and return
    ' the file handler.  The file can be 
    ' gzipped or not.
    '''
    if aFilename.endswith('.gz'):
        return gzip.open(aFilename,'rb')
    else:
        return open(aFilename,'r')


def get_write_fileHandler(aFilename):
    '''
    ' Open aFilename for writing and return
    ' the file handler.  The file can be 
    ' gzipped or not.
    '''
    if aFilename.endswith('.gz'):
        return gzip.open(aFilename,'wb')
    else:
        return open(aFilename,'w')
    

def get_chrom_size(aChrom, anInputStream, anIsDebug):
    '''
    ' This function reads from a FASTA index file or a tab separated file that contains 
    ' the sizes for each chromosome. The file has a minimum of two columns:  the first 
    ' column has the chromosome identifier, the second column has the size of the chromosome.  
    ' The columns are separated by tabs.
    ' Here is an example:
    ' chr1    249250621
    ' chr2    243199373
    ' ...       ...
    '
    ' aChrom: The chrom size to return
    ' anInputStream: The input stream for the chrom sizes file
    '''
     
    for line in anInputStream:
          
        # if it is an empty line, then just continue
        if (line.isspace() or line.startswith("#")):
            continue;

        # strip the carriage return and newline characters
        line = line.rstrip("\r\n")

        if (anIsDebug):
            logging.debug("get_chrom_size(): looking for size of chrom %s, line=%s", aChrom, line)	
        
        # split the line on the tab
        splitLine = line.split("\t")
        
        # the coordinate is the second element
        chrom = splitLine[0]
        size = int(splitLine[1])
        
        # sometimes the chroms have the "chr" prefix, sometimes they don't
        if (chrom == aChrom or chrom == "chr" + str(aChrom)):
            if (anIsDebug):
                logging.debug("get_chrom_size(): found size of chrom %s, size=%s", aChrom, size)
            return size
        
    return -1


def get_batch_end_coordinate(aStartCoordinate, anEndCoordinate, aBatchSize):
    '''
    ' This function takes a start coordinate, an end coordinate, and a batch size and
    ' returns the next appropriate batch end coordinate which is either the start coordinate
    ' plus the batch size if this is less than the final end coordinate otherwise the end
    ' coordinate.
    '
    ' aStartCoordinate:  A start coordinate
    ' anEndCoordinate:  A stop coordinate
    ' aBatchSize:  A batch size
    '''
    if ((aStartCoordinate + aBatchSize) < anEndCoordinate):
        # we don't want to have the end coordinate be the same as the next batch's start coordinate
        # so make sure to do a minus one here
        return (aStartCoordinate + aBatchSize - 1)
    else:
        return (anEndCoordinate)
    

def get_sam_data(aSamFile, aChrom, aStartCoordinate, aStopCoordinate, aSourcePrefix, anIsDebug):
    '''
    ' This function is used during testing to read data from a .sam input file.  It uses the python 
    ' generator to yield the information for one coordinate position at a time.  This function yields 
    ' the chromosome, coordinate, reference base, number of reads, raw reads, and quality scores.
    '
    ' aSamFile:  A .sam file
    '''
    
    # open the sam file
    samFileHandler = get_read_fileHandler(aSamFile)
     
    for line in samFileHandler:
          
        # if the samtools select statement returns no reads which can happen when the batch size is
        # small and the selection is done in an area with no reads, then a warning message will be
        # returned that starts with "[mpileup]".  We can ignore the message and move on to the next
        # select statement.
        if (line.isspace() or line.startswith("[mpileup]")):
            continue;

        # strip the carriage return and newline characters
        line = line.rstrip("\r\n")

        if (anIsDebug):
            logging.debug("Original SAM pileup on %s: %s", aSourcePrefix, line)	
        
        # split the .sam line on the tab
        splitLine = line.split("\t")
        
        if (len(splitLine) > 1):
            # the coordinate is the second element
            chrom = splitLine[0]
            coordinate = int(splitLine[1])
            if (coordinate < aStartCoordinate):
                continue
            if (coordinate > aStopCoordinate):
                break
            reference = splitLine[2].upper()
            numOfReads = int(splitLine[3])
            reads = splitLine[4]
            qualScores = splitLine[5]
        else:
            continue
       
        # yield all the information about the current coordinate
        yield (chrom, coordinate, reference, numOfReads, reads, qualScores)

    samFileHandler.close()
    return


def get_bam_data(aBamFile, aFastaFile, aBaseQual, aMappingQual, aChrom, aStartCoordinate, aStopCoordinate, aBatchSize, aUseChrPrefix, aSourcePrefix, anIsDebug):
    '''
    ' This function uses the python generator to yield the information for one coordinate at a time.
    ' In order to reduce the time and memory overhead of loading the entire .bam file into memory at
    ' once, this function reads in chunks of data at a time.  The number of coordinates that should be
    ' read into memory at a given time is determined by the "aBatchSize" parameter.  This function uses the 
    ' samtools "mpileup" command to make a selection. 
    '
    ' The original start and end coordinates are specified by the "aStartCoordinate" and "anEndCoordinate" 
    ' parameters and are typically initialized to 0 and the size of the chromosome respectively. This function
    ' will loop over the .bam file, selecting "aBatchSize" number of coordinates into memory at once.  Each line that
    ' is selected will be processed and yielded using the python generator.  When all lines from the current batch 
    ' are processed, the start and end coordinates will be incremented, and the next selection will be made from the
    ' .bam file.  This process continues until the end of the chromosome has been reached.
    '
    ' This function yields the chromosome, coordinate, reference base, number of reads, raw reads, and the quality scores.
    '
    ' aBamFile:            A .bam file to be read from
    ' aFastaFile:          The FASTA file that should be used in the samtools command which is needed for the reference base.
    ' aBaseQual:           The base quality score that should be used in the samtools command
    ' aMappingQual:        The mapping quality score that should be used in the samtools command
    ' aChrom:              The chromosome that should be used in the samtools command
    ' aStartCoordinate:    The initial start coordinate (typically zero)
    ' aStopCoordinate:     The initial stop coordinate (typically the size of the chromosome)
    ' aBatchSize:          The number of coordinates to load into memory at one time
    ' aUseChrPrefix:       Whether the 'chr' should be used in the region parameter of the samtools command
    '''
    
    # initialize the first start and stop coordinates
    # the stop coordinate is calculated according to the "aBatchSize" param
    currentStartCoordinate = aStartCoordinate
    currentStopCoordinate = get_batch_end_coordinate(currentStartCoordinate, aStopCoordinate, aBatchSize)

    # while we still have coordinates to select from the .bam file
    while (currentStartCoordinate < aStopCoordinate):
        # execute the samtools command
        pileups = execute_samtools_cmd(aBamFile, aFastaFile, aBaseQual, aMappingQual, aChrom, aUseChrPrefix, currentStartCoordinate, currentStopCoordinate, anIsDebug)
        
        numPileups = 0
        
        # for each line representing one coordinate
        for line in pileups:
                
            # if the samtools select statement returns no reads which can happen when the batch size is
            # small and the selection is done in an area with no reads, then a warning message will be
            # returned that starts with "[mpileup]".  We can ignore the message and move on to the next
            # select statement.
            if (line.isspace() or line.startswith("[mpileup]") or line.startswith("<mpileup>")):
                continue;

            # strip the carriage return and newline characters
            line = line.rstrip("\r\n")

            # split the line on the tab
            splitLine = line.split("\t")
            
            if (anIsDebug):    
                logging.debug("Original BAM pileup for %s: %s", aSourcePrefix, line)

            if (len(splitLine) > 1):
                # the coordinate is the second element
                chrom = splitLine[0]
                coordinate = int(splitLine[1])
                reference = splitLine[2].upper()
                numOfReads = int(splitLine[3])
                reads = splitLine[4]
                qualScores = splitLine[5]
            else:
                continue
                    
            # yield all the information about the current coordinate
            yield (chrom, coordinate, reference, numOfReads, reads, qualScores)
            numPileups += 1

        if (anIsDebug):        
            logging.debug("samtools number of lines selected from %s to %s = %s", currentStartCoordinate, currentStopCoordinate, numPileups)
        
        # calculate a new start and stop coordinate for the next select statement
        currentStartCoordinate = currentStartCoordinate + aBatchSize
        currentStopCoordinate = get_batch_end_coordinate(currentStartCoordinate, aStopCoordinate, aBatchSize)
    
    return


def execute_samtools_cmd(aBamFile, aFastaFile, aBaseQuality, aMappingQuality, aChrom, aUseChrPrefix, aStartCoordinate, aStopCoordinate, anIsDebug):
    '''
    ' This function executes an external command.  The command is the "samtools mpileup" command which returns all 
    ' the information about the sequencing reads for specific coordinates.  There are two things to be careful about
    ' when using the samtools mpileup command.  Some .bam files use the 'chr' prefix when specifying the region to 
    ' select with the -r argument.  If the 'chr' prefix is required, then specify the --useChrPrefix argument and also
    ' make sure that the fasta file that is specified also has the 'chr' prefix.  Here are some examples of the commands
    ' that can be used to view the output:
    '
    ' samtools mpileup -f /path/to/fasta/hg19.fa -Q 20 -q 10 -r chr1:855155-1009900 /path/to/bams/myBam.bam
    ' samtools mpileup -f /path/to/fasta/hg19.fa -Q 20 -q 10 -r 1:855155-1009900 /path/to/bams/myBam.bam
    '
    ' aBamFile:            A .bam file to be read from
    ' aFastaFile:          The FASTA file which is needed for the reference base.
    ' aBaseQuality:        The base quality score for the samtools command
    ' aMappingQuality:     The mapping quality score for the samtools command
    ' aChrom:              The chromosome that we are selecting from
    ' aStartCoordinate:    The start coordinate of the selection
    ' aStopCoordinate:     The stop coordinate of the selection
    ' aUseChrPrefix:       Whether the 'chr' should be used in the samtools command
    '''
    # create the samtools command
    if (aUseChrPrefix):
        samtoolsSelectStatement = "samtools mpileup -E -f " + aFastaFile + " -Q " + str(aBaseQuality) + " -q " + str(aMappingQuality) + " -r chr" + aChrom + ":" + str(aStartCoordinate) + "-" + str(aStopCoordinate) + " " + aBamFile
    else:
        samtoolsSelectStatement = "samtools mpileup -E -f " + aFastaFile + " -Q " + str(aBaseQuality) + " -q " + str(aMappingQuality) + " -r " + aChrom + ":" + str(aStartCoordinate) + "-" + str(aStopCoordinate) + " " + aBamFile
    
    # output the samtools command
    if (anIsDebug):
        logging.debug(samtoolsSelectStatement)
    
    # execute the samtools command
    timeSamtoolsStart = time.time()
    samtoolsCall = subprocess.Popen(samtoolsSelectStatement, shell=True, bufsize=4096, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    #samtoolsCall = subprocess.Popen(samtoolsSelectStatement, shell=True, bufsize=-1, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
   
    for line in samtoolsCall.stdout:
        yield line

    # communicate() waits for the process to finish
    #(pileups, samtoolsStdErr) = samtoolsCall.communicate()
    samtoolsStdErr = samtoolsCall.wait()

    timeSamtoolsEnd = time.time()
    timeSpent = timeSamtoolsEnd-timeSamtoolsStart
    
    if (anIsDebug):
        logging.debug("Time spent executing samtools command: %s hrs, %s mins, %s secs", (timeSpent/3600), (timeSpent/60), (timeSpent))  
    
    # if the return code is None, then the process is not yet finished
    # communicate() waits for the process to finish, poll() does not
    if (samtoolsCall.returncode == None):
        logging.warning("The samtools mpileup command is not done, indicating an error.")
    # if samtools returned a return code != 0, then an error occurred
    # warning: previous versions of samtools did not return a return code!
    elif (samtoolsCall.returncode != 0):
        logging.warning("The return code of '%s' from the samtools mpileup command indicates an error.", samtoolsCall.returncode)
        logging.warning("Warning/error from %s:\n%s", samtoolsSelectStatement, samtoolsStdErr)
    return


def convert_raw_reads(aChr, aCoordinate, aStringOfRawReads, aStringOfQualScores, aReferenceBase, anIsDebug):
    '''
    ' This function returns all of the valid RNA (cDNA) or DNA bases from the given pileup of read bases.
    ' It converts all of the samtools specific characters into human-readable bases and filters out any non 
    ' RNA/DNA characters. 
    '
    ' This is from the samtools documentation:
    '
    ' In the pileup format, each line represents a genomic position, consisting of chromosome name, 
    ' 1-based coordinate, reference base, read bases, read qualities and alignment mapping qualities. 
    ' Information on match, mismatch, indel, strand, mapping quality and start and end of a read are all 
    ' encoded at the read base column. At this column, a dot stands for a match to the reference base on 
    ' the forward strand, a comma for a match on the reverse strand, a ">" or "<" for a reference skip, 
    ' "ACGTN" for a mismatch on the forward strand and "acgtn" for a mismatch on the reverse strand. A 
    ' pattern "\+[0-9]+[ACGTNacgtn]+" indicates there is an insertion between this reference position and 
    ' the next reference position. The length of the insertion is given by the integer in the pattern, 
    ' followed by the inserted sequence. Similarly, a pattern "-[0-9]+[ACGTNacgtn]+" represents a deletion 
    ' from the reference. The deleted bases will be presented as "*" in the following lines. Also at the 
    ' read base column, a symbol "^" marks the start of a read. The ASCII of the character following "^" 
    ' minus 33 gives the mapping quality. A symbol "$" marks the end of a read segment.
    '
    ' We are converting all dots and commas to the upper case reference base.  Even though the comma represents 
    ' a match on the reverse strand, there is no need to take the complement of it, since samtools does
    ' that for us.  We are converting all mismatches on the reverse strand to upper case as well, and again 
    ' no complement is needed.
    '
    ' We are ignoring the following for now:
    ' 1) Reference skips (">" and "<") 
    ' 2) "N" in the reads
    '
    ' aStringOfRawReads: A string representing the pile-up of read bases from a samtools mpileup command 
    ' aStringOfQualScores: A string representing the raw quality scores for the read bases from the mpileup command
    ' aReferenceBase: Used to convert "." and "," from the samtools mpileup command
    '''
    
    # Note:  Reverse strand mismatches have been reverse-complemented by samtools
    
    # initialize some counts
    indelCount = 0
    
    # remove insertions and deletions
    # a read could look like:  "T$TT+3AGGGT+2AG+2AG.-2AGGG..-1A"
    # insertions start with a "+", deletions with a "-"
    # in theory, there could be multiple digits
    # i_numOfIndelsRegEx = re.compile("[+-]{1}(\\d+)")

    # if we have an indel
    if ("+" in aStringOfRawReads or "-" in aStringOfRawReads):
        # get an iterator of match objects for all indels
        iterator = i_numOfIndelsRegEx.finditer(aStringOfRawReads)
        
        # for each match object in the iterator
        for match in iterator:
            indelCount += 1
            # get the pattern that matched the reg ex, i.e. +3 or -2
            indel = match.group()
            # the length of the indel is the number following the + or - sign
            lengthOfIndel = indel[1:len(indel)]
            # this reg ex will specifically match indels with the specified length, i.e. +3AGG, -2AG
            indelRegEx = re.compile("\\" + indel + "[ACGTNacgtn=]{" + lengthOfIndel + "}")
            
            # we can simply remove the indels and replace them with empty strings for now
            # there are no base quality scores for indels that need to be removed
            aStringOfRawReads = indelRegEx.sub("", aStringOfRawReads) 
            
        if (indelCount > 0):
            logging.debug("%s indels found in %s", indelCount, aStringOfRawReads)
            
    # count starts and stops
    starts = aStringOfRawReads.count("^")
    stops = aStringOfRawReads.count("$")
        
    # remove all start of read symbols "^" (plus the following quality score)
    # there are no base quality scores for start symbols that need to be removed
    while ("^" in aStringOfRawReads):
        start = aStringOfRawReads.find("^")
        end = start+2
        # replace will replace all unless a max is set, but we don't care, 
        # we want to get rid of all of them
        aStringOfRawReads = aStringOfRawReads.replace(aStringOfRawReads[start:end], "")
    
    # remove all end of read symbols "$"
    # there are no base quality scores for stop symbols that need to be removed
    aStringOfRawReads = aStringOfRawReads.replace("$", "")
    
    # replace all the periods for uppercase references representing the plus strand
    # replace all the commas for lowercase references representing the minus strand
    aStringOfRawReads = aStringOfRawReads.replace(".", aReferenceBase.upper())
    aStringOfRawReads = aStringOfRawReads.replace(",", aReferenceBase.lower())
        
    # get an iterator of match objects for all valid cDNA
    # this regular expression will match any number of valid cDNA strings
    # i_cDNARegEx = re.compile("[ACGTacgt]+")
    iterator = i_cDNARegEx.finditer(aStringOfRawReads)
    
    # create final strings consisting of just the valid cDNA and corresponding qual scores
    finalPileups = ""
    finalQuals = ""
    
    # only extract the valid cDNA and corresponding qual scores
    # ignore >", "<", etc.
    for match in iterator:
        start = match.start()
        end = match.end()
        finalPileups += aStringOfRawReads[start:end]
        finalQuals += aStringOfQualScores[start:end]
              
    # get the lengths
    lenFinalPileups = len(finalPileups)
    lenFinalQuals = len(finalQuals) 
    
    # at this point, the length of the pileups string should be equal to the length of the quality scores
    if (lenFinalPileups != lenFinalQuals):
        logging.error("Traceback: convert_raw_reads() Error at coordinate %s:%s.  The length %s of the final pileup of reads is != the length %s of the final quality scores.  Original Pileup=%s, Final Pileup=%s, Original QualScores=%s, Final QualScores=%s", aChr, str(aCoordinate), lenFinalPileups, lenFinalQuals, aStringOfRawReads, finalPileups, aStringOfQualScores, finalQuals)
     
    return (finalPileups, finalQuals, lenFinalPileups, starts, stops, indelCount)     


def filter_by_base_quality(aStringOfReads, aStringOfQualScores, aMinBaseQualityScore, anIsDebug):
    '''
    ' This function filters out all the bases that don't meet the user-specified minimum 
    ' base quality score which is specified here with the "aMinBaseQualityScore" parameter.
    '
    ' aStringOfReads: A string representing the pile-up of reads from a samtools mpileup command
    ' aStringOfQualScores: A string representing the raw quality scores for the read bases from the mpileup command 
    ' aMinBaseQualityScore: An integer with the user-specified minimum base quality score (also used as -Q parameter to samtools mpileup command)
    '''
    
    # create strings consisting of just the reads that are greater than or equal to the minimum base quality score 
    pileups = ""
    qualScores = ""
    numBasesDict = collections.defaultdict(int)
    sumBaseQualsDict = collections.defaultdict(int)
    numPlusStrandDict = collections.defaultdict(int)
                
    # loop through the reads and the corresponding quality scores
    for (base, rawScore) in izip(aStringOfReads, aStringOfQualScores):
        convertedScore = ord(rawScore)-33
        # the scores are in ascii, so convert them to integers
        if (convertedScore >= aMinBaseQualityScore):
            
            # count the ones on the plus strand
            if (base in "ACGTN"):
                numPlusStrandDict[base] += 1
            # convert all to plus strand after counting
            else:
                base = base.upper()
            
            # keep the base and quality
            pileups += base
            qualScores += rawScore
            
            # keep track of the number of each base and the corresponding qual score
            numBasesDict[base] += 1
            sumBaseQualsDict[base] += convertedScore
                
    return (pileups, qualScores, len(pileups), numBasesDict, sumBaseQualsDict, numPlusStrandDict)               


def format_bam_output(aChrom, aRefList, anAltList, anAltCountsDict, anAltPerDict, aStringReads, aStringQualScores, aNumBases, aStartsCount, aStopsCount, anIndelCount, aBaseCountsDict, aQualitySumsOfBasesDict, aPlusStrandCountsDict, aGTMinDepth, aGTMinPct, anIsDebug):
    '''
    ' This function converts information from a .bam mpileup coordinate into a format that can be output to a VCF formatted file.
    ' This function calculates the average overall base quality score, strand bias, and fraction of reads supporting the alternative.
    ' It also calculates the allele specific depth, average base quality score, strand bias, and fraction of reads supporting the alternative.
    ' The format for the output in VCF is:  GT:DP:INDEL:START:STOP:AD:AF:BQ:SB.
    '
    ' aDnaSet:  A set of dna found at this position
    ' anAltList: A list of alternative alleles found thus far
    ' aStringReads:  A string of reads that have been converted from raw format and filtered
    ' aStringQualScores: A string of quality scores for the reads
    ' aStartsCount:  The number of bases that were at the start of the read
    ' aStopsCount:  The number of bases that were at the stop of the read
    ' anIndelCount:  The number of indels at this position
    ' aBaseCountsDict:  A dictionary with the number of bases of each type
    ' aQualitySumsOfBasesDict:  A dictionary with the sum of all quality scores for each type of base
    ' aPlusStrandCountsDict:  The number of bases that occurred on the plus strand
    '''
    
    # initialize the return variables
    returnString = "."
    sumAltReadSupport = 0
    
    # if we have reads at this position
    if (aNumBases > 0):
        
        #format = "GT:DP:INDEL:START:STOP:AD:AF:BQ:SB"
        
        #vcfHeader += "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
        #vcfHeader += "##FORMAT=<ID=DP,Number=1,Type=Integer,Description=\"Read depth\">\n"
        #vcfHeader += "##FORMAT=<ID=INDEL,Number=1,Type=Integer,Description=\"Number of indels\">\n"
        #vcfHeader += "##FORMAT=<ID=START,Number=1,Type=Integer,Description=\"Number of reads starting at this position\">\n"
        #vcfHeader += "##FORMAT=<ID=STOP,Number=1,Type=Integer,Description=\"Number of reads stopping at this position\">\n"
        #vcfHeader += "##FORMAT=<ID=AD,Number=.,Type=Float,Description=\"Depth of reads supporting alleles 0/1/2/3\">\n"
        #vcfHeader += "##FORMAT=<ID=AF,Number=.,Type=Float,Description=\"Fraction of reads supporting alleles 0/1/2/3\">\n"
        #vcfHeader += "##FORMAT=<ID=BQ,Number=.,Type=Float,Description=\"Avg base quality for reads supporting alleles 0/1/2/3\">\n"
        #vcfHeader += "##FORMAT=<ID=SB,Number=.,Type=Float,Description=\"Strand Bias for reads supporting alleles 0/1/2/3\">\n"
            
        # initialize some lists
        depths = list()
        readSupports = list()
        baseQuals = list()
        strandBias = list()
        altCountsDict = {}
        
        # for each base in the ref list and alt list
        # the order matters for the output
        for base in (aRefList + anAltList):
            
            # get the number of times the base occurs
            count = aBaseCountsDict[base]
            depths.append(count)

            # calculate the allele specific fraction of read support
            readSupport = round(count/float(aNumBases), 2)
            readSupports.append(readSupport)
            
            # if the base is an alt, then count it for the overall read support
            if (base in anAltList):
                sumAltReadSupport += count
                anAltCountsDict[base] += count
                # we need just the alt counts for the genotypes code below
                altCountsDict[base] = count
            
            # calculate the allele specific avg quality and plus strand scores
            if (count > 0):
                avgBaseQuality = round(aQualitySumsOfBasesDict[base]/float(count),2)
                avgPlusStrandBias = round(aPlusStrandCountsDict[base]/float(count),2)
            else:
                avgBaseQuality = 0.0
                avgPlusStrandBias = 0.0
                
            baseQuals.append(avgBaseQuality)
            strandBias.append(avgPlusStrandBias)

            
        # get the genotype:
        #    if chrom Y
        #        then genotype = the ref or alt with the max read depth
        #    if there are only reads for ref
        #        then genotype = 0/0
        #    if there are only reads for alt
        #        then genotype = 1/1
        #    if there are reads for both ref and alt above the min depth and percent, then pick the ones with max counts
        #        then genotype = 0/1
        #    if chrom M or MT
        #        then any allele above the min depth and percent can be listed
        genotypes = None
        refAltList = aRefList + anAltList
        singleGenotypeChroms = ["chrY", "Y"]
        mChroms = ["chrM", "chrMT", "M", "MT"]
        
        # if it is a single chrom, then we can only assign one allele for the genotype
        # if one of the alts has a depth and percent above the mins, then use it, otherwise use the ref
        if (aChrom in singleGenotypeChroms):
            if aBaseCountsDict:
                
                # get the total depth
                totalDepth = sum(aBaseCountsDict.itervalues())
                    
                # if we have some alts
                if altCountsDict:
                    # find the max alt allele    
                    (maxAltBase, maxAltDepth) = max(altCountsDict.iteritems(), key=lambda x:x[1])
                    maxAltPct = round(maxAltDepth/float(totalDepth), 2)
                    
                    # if the max alt depth is large enough
                    if (maxAltDepth >= aGTMinDepth and maxAltPct >= aGTMinPct):
                        # find the index for the max depth on the original list
                        maxAltIndex = refAltList.index(maxAltBase)
                    else:
                        # it wasn't large enough, so just use the ref
                        maxAltIndex = 0
                else:
                    # no alts, so just use the ref
                    maxAltIndex = 0
                
                # set the single genotype    
                genotypes = [maxAltIndex]
                
            else:
                # we don't have any bases, so just set it to the ref
                genotypes = [0]
        
        # if it is an M chrom, then we can assign as many alleles as we want for the genotype
        # for all bases with a depth and percent above the mins, set the genotype
        elif (aChrom in mChroms):
            if aBaseCountsDict:
                
                # get the total depth
                totalDepth = sum(aBaseCountsDict.itervalues())
                
                tmpGenotypes = []
                # for each base in the ref and alt
                for (base, depth) in aBaseCountsDict.iteritems():
                    # calculate the percent
                    percent = round(depth/float(totalDepth), 2)
                    # if the max alt depth and percent are large enough
                    if (depth >= aGTMinDepth and percent >= aGTMinPct):
                        # add the index to the list
                        index = refAltList.index(base)
                        tmpGenotypes.append(index)
                        
                # if nothing passed the mins, then just take the ref
                if (len(tmpGenotypes) == 0):
                    tmpGenotypes = [0]
                    
                genotypes = sorted(tmpGenotypes)
            else:
                # we don't have any bases, so just set it to the ref
                genotypes = [0]
                    
        # if it is a diploid chrom, then assign the 2 max counts above the min cutoffs
        else:
            # get the total depth
            totalDepth = sum(aBaseCountsDict.itervalues())
    
            # make a copy of the dict to manipulate
            baseCountsTmpDict = dict(aBaseCountsDict)
            
            # get the max depth
            (max1Base, max1Depth) = max(baseCountsTmpDict.iteritems(), key=lambda x:x[1])
            
            # find the index for the max depth on the original list
            max1DepthIndex = refAltList.index(max1Base)
        
            # remove the max from the tmp list
            del baseCountsTmpDict[max1Base]
            
            # if we still have some depths, find the 2nd max
            if baseCountsTmpDict:
            
                # get the max depth
                (max2Base, max2Depth) = max(baseCountsTmpDict.iteritems(), key=lambda x:x[1])
                max2Pct = round(max2Depth/float(totalDepth), 2)
                
                # if the max depth is large enough
                if (max2Depth >= aGTMinDepth and max2Pct >= aGTMinPct):
                    # find the index for the max depth base on the original list
                    # note: here we are using the dictionary of base=count, so we
                    # can specifically ask for the base.  In subsequence genotypes()
                    # methods, we only have the depths without the corresponding base
                    # so we have to pay extra attention when the depths are equal
                    max2DepthIndex = refAltList.index(max2Base)
                else:
                    # it wasn't large enough, so just use previous max
                    max2DepthIndex = max1DepthIndex
                
            else:
                # otherwise it's the same as the first
                max2DepthIndex = max1DepthIndex
            
            genotypes = sorted([max1DepthIndex, max2DepthIndex])
        
        # create a list of each of the elements, then join them by colon
        outputList = ("/".join(map(str, genotypes)), str(aNumBases), str(anIndelCount), str(aStartsCount), str(aStopsCount), ",".join(map(str, depths)), ",".join(map(str, readSupports)), ",".join(map(str, baseQuals)), ",".join(map(str, strandBias)))
        returnString = ":".join(outputList)
        
    # return the string representation and overall calculations       
    return (returnString, anAltCountsDict, anAltPerDict, sumAltReadSupport)


def get_next_pileup(aGenerator):
    '''
    ' This function returns the next pileup from a generator that yields pileups.  If the user doesn't
    ' specify all four BAM files, then the generator will be "None", so just return some default values.
    ' If we reach the end of a file, the generator will throw a StopIteration, so just catch it and 
    ' return some default values.  Otherwise, return the appropriate pileup information.
    '
    ' aGenerator:  A .bam mpileup generator that yields the next pileup
    '''
    
    if (aGenerator == None):
        return False, "", -1, "", 0, "", ""
    else:
        try:
            # get the next line
            (chrom, coordinate, refBase, numReads, reads, qualScores) = aGenerator.next() 
            return True, chrom, int(coordinate), refBase, int(numReads), reads, qualScores                     
        except StopIteration:
            return False, "", -1, "", 0, "", ""


def find_variants(aChr, aCoordinate, aRefBase, aNumBases, aReads, aBaseQuals, aPreviousUniqueBases, aPreviousBaseCounts, aReadDepthDict, anAltPerDict, aCoordinateWithData, aDnaSet, aRefList, anAltList, anAltCountsDict, aHasValidData, aShouldOutput, aGainModCount, aLossModCount, aGainModType, aLossModType, anInfoDict, aMinTotalNumBases, aMinAltNumBases, aBaseQual, aBaseQualsList, aSourcePrefix, aGTMinDepth, aGTMinPct, anIsDebug):
    '''
    ' This function finds variants in BAM pileups.  This function first converts the samtools pileup of reads into 
    ' human-readable reads and then records some characteristics of the pileups.  It counts the number of bases on the 
    ' plus and minus strands, the number of bases at the start and end of reads, and the number of indels.  This function 
    ' then ensures that the bases in the reads pass the minimum base quality score.  If the number of remaining bases is 
    ' greater than or equal to the minimum total of bases specified by 'aMinTotalNumBases', then this function looks 
    ' to see if there are any variants in the data.
    '
    ' The 'aDnaSet' object is empty when processing normal DNA.  This function automatically adds the reference base, so no
    ' pre-processing of aDnaSet is needed.  After the reference has been added, the function looks for variants in the reads.  If a base
    ' is not in aDnaSet and there are at least 'aMinAltNumBases' of them, then this function adds the variant to 'aModTypesSet'.  After 
    ' all the variants have been processed, the unique reads at this position are added to 'aDnaSet' which is used in the next steps: 
    ' looking for somatic variations and rna-editing events.
    '
    ' This function returns:
    ' bamOutputString - The concatenated form of the pileup data at this position
    ' aDnaSet - A set of 'parent' DNA {Ref for germline variants, ref + normal for somatic mutations, ref + normal + tumor for rna-editing}
    ' aHasValidData - If there was valid data at this position
    ' aShouldOutput - If there were any variants found to be output
    ' aModCount - The number of variants found
    ' aModTypesSet - The set of variants found
    '''
    
    # default outputs
    bamOutputString = "."
    sumOfBaseQuals = 0
    sumOfStrandBiases = 0
    sumOfAltReads = 0
    oneAboveMinAltBasesFlag = False
    setBelowMinAltBasesFlag = False 
    uniqueBases = ""
    baseCountsDict = collections.defaultdict(int)
    
    # convert the raw reads into human-readable reads
    (convertedReads, convertedBaseQuals, aNumBases, starts, stops, indels) = convert_raw_reads(aChr, aCoordinate, aReads, aBaseQuals, aRefBase, anIsDebug)
    
    # keep track of the read coverage
    #aReadDepthDict[aNumBases] += 1

    if (anIsDebug):    
        logging.debug("After convert_raw_reads() on %s: %s %s %s %s %s %s %s %s %s", aSourcePrefix, aChr, aCoordinate, aRefBase, aNumBases, convertedReads, convertedBaseQuals, starts, stops, indels)
    
    # if we still have some bases
    if (aNumBases > 0):
        
        # filter out the bases that don't meet the minimum base quality scores
        (convertedReads, convertedBaseQuals, aNumBases, baseCountsDict, qualitySumsOfBasesDict, plusStrandCountsDict) = filter_by_base_quality(convertedReads, convertedBaseQuals, aBaseQual, anIsDebug)  
        
        if (anIsDebug):
            logging.debug("After filter_by_base_quality() on %s: %s %s %s %s %s %s %s %s %s %s %s %s", aSourcePrefix, aChr, aCoordinate, aRefBase, aNumBases, convertedReads, convertedBaseQuals, starts, stops, indels, baseCountsDict, qualitySumsOfBasesDict, plusStrandCountsDict)
        
        # if we still have some bases
        if (aNumBases > 0):
            aCoordinateWithData += 1
            
            # keep track of the base quals for later filtering
            #aBaseQualsList.append([aSourcePrefix, aChr, str(aCoordinate), aReads, aBaseQuals, convertedReads, convertedBaseQuals])
            
            # if the dna set is empty, then none of the previous samples had data
            # add the reference below, and set all bases to equal aMinAltNumBases 
            # because we assume that the reference or "neutral" sample had enough
            # previous bases for all bases
            if (len(aDnaSet) == 0):
                for base in ('ACTG'):
                    # we can do this, b/c baseCountsDict gets returned as the previousCounts
                    # for this sample.  we can't do this if aPreviousBaseCounts should hold
                    # the counts for all previous samples.
                    aPreviousBaseCounts[base] = aMinAltNumBases
                    
            # add the reference base
            aDnaSet.add(aRefBase)
            # we always have enough of the ref base
            aPreviousBaseCounts[aRefBase] = aMinAltNumBases
            
            # for each unique base
            for base in set(convertedReads):
                
                # keep track of every ALT base in the order that it's found
                if (base not in aDnaSet and base not in anAltList):
                    anAltList.append(base)
                
                # if we have enough total bases
                if (aNumBases >= aMinTotalNumBases):
                
                    aHasValidData = True
                    
                    # keep track of every unique base
                    uniqueBases += base
                    
                    # if there is a base that wasn't in the previous sample,
                    # or the base was in the previous sample, but there weren't enough to make a call
                    if ((base not in aDnaSet) or (aPreviousBaseCounts[base] < aMinAltNumBases)):            
                        
                        # if we have enough ALT bases
                        if (baseCountsDict[base] >= aMinAltNumBases):
                            oneAboveMinAltBasesFlag = True
                            aShouldOutput = True
                            #aGainModCount += 1
                            
                            if (anIsDebug):
                                logging.debug("Modification found!  Base '%s' from the reads does not exist in the parent DNA set %s", base, aDnaSet)
                                
                            # if this is the first modification found, then record it in the "SS" field
                            if (len(anInfoDict["SS"]) == 0):
                                if (aGainModType == "GERM"):
                                    anInfoDict["SS"].append("1")
                                elif (aGainModType == "SOM"):
                                    anInfoDict["SS"].append("2")
                                    anInfoDict["SOMATIC"].append("True")
                                elif (aGainModType.find("EDIT") != -1):
                                    anInfoDict["SS"].append("5")
                                # other
                                else:
                                    anInfoDict["SS"].append("4")
                            
                            # it didn't match anything so far
                            for dna in aDnaSet:
                                # check to see which parent bases were above the minimum
                                if (aPreviousBaseCounts[dna] >= aMinAltNumBases):
                                    anInfoDict["MT"].append(aGainModType)
                                    anInfoDict["MC"].append(dna + ">" + base)
                                    aGainModCount += 1
                        else:
                            setBelowMinAltBasesFlag = True
                     
                    '''            
                    # check for LOH's
                    if ("GERM" in anInfoDict["MT"]):
                        for base in aPreviousUniqueBases:
                            
                            # if we had enough previous germline bases, and they are now below the minimum
                            if ((aPreviousBaseCounts[base] >= aMinAltNumBases) and (baseCountsDict[base] < aMinAltNumBases)):
                                aShouldOutput = True
                                aLossModCount += 1
                                
                                if (anIsDebug):
                                    logging.debug("Loss found!  Base '%s' from the parent DNA set no longer exists in the child reads %s", base, convertedReads)
                                
                                # remove the previous germline SS
                                if ("1" in anInfoDict["SS"]):
                                    germIndex = anInfoDict["SS"].index("1")
                                    anInfoDict["SS"].pop(germIndex)
                                
                                # set the SS to LOH
                                if (aLossModType == "LOH"):
                                    anInfoDict["SS"].append("3")
                                else:
                                    anInfoDict["SS"].append("4")
                            
                                # there could be more than one germline, so just remove them all
                                while ("GERM" in anInfoDict["MT"]):
                                    # remove the previous germline mutations       
                                    germIndex = anInfoDict["MT"].index("GERM")
                                    anInfoDict["MT"].pop(germIndex)
                                    anInfoDict["MC"].pop(germIndex)
                                    
                                # add the LOH
                                anInfoDict["MT"].append(aLossModType)
                                previousBasesAboveCutoff = ""
                                for previousBase in aPreviousUniqueBases:
                                    if (aPreviousBaseCounts[previousBase] >= aMinAltNumBases):
                                        previousBasesAboveCutoff += previousBase
                                
                                # output the previous bases and the current unique bases
                                anInfoDict["MC"].append(previousBasesAboveCutoff + ">" + uniqueBases)
                    '''  
            # add the unique reads for the next step
            aDnaSet = aDnaSet.union(set(convertedReads))
            
            # get the summary output for the pileups at this position
            (bamOutputString, anAltCountsDict, anAltPerDict, sumOfAltReads) = format_bam_output(aChr, aRefList, anAltList, anAltCountsDict, anAltPerDict, convertedReads, convertedBaseQuals, aNumBases, starts, stops, indels, baseCountsDict, qualitySumsOfBasesDict, plusStrandCountsDict, aGTMinDepth, aGTMinPct, anIsDebug)
            
            sumOfBaseQuals = sum(qualitySumsOfBasesDict.itervalues())
            sumOfStrandBiases = sum(plusStrandCountsDict.itervalues())
                 
    return (bamOutputString, uniqueBases, baseCountsDict, aReadDepthDict, anAltPerDict, aCoordinateWithData, aDnaSet, anAltList, anAltCountsDict, aHasValidData, aShouldOutput, (aNumBases < aMinTotalNumBases), (not oneAboveMinAltBasesFlag and setBelowMinAltBasesFlag), aGainModCount, aLossModCount, anInfoDict, aNumBases, indels, starts, stops, sumOfBaseQuals, sumOfStrandBiases, sumOfAltReads, aBaseQualsList)
        
        
def get_vcf_header(aVCFFormat, aRefId, aRefURL, aRefFilename, aFastaFilename, aRadiaVersion, aPatientId, aParamDict, aFilenameList, aLabelList, aDescList, aPlatformList, aSourceList, aDisease, anIsDebug):
    '''
    ' This function creates a VCF header that is used for the output.
    '
    ' aVCFFormat - The current file format version
    ' aRefId - The short reference id such hg18, hg19, GRCh37
    ' aRefURL - The URL for the reference file provided
    ' aRefFilename - The filename of the reference
    ' aRadiaVersion - The version of RADIA
    ' aPatientId - The unique patient Id to be used in the SAMPLE tag
    ' aParamDict - Used to record the parameters that were used to run RADIA and generate the VCF file.
    ' aFilenameList - Used in the SAMPLE tag
    ' aLabelList - Used in the SAMPLE tag
    ' aDescList - Used in the SAMPLE tag
    ' aPlatformList - Used in the SAMPLE tag
    ' aSourceList - Used in the SAMPLE tag
    '''
    
    # initialize the column headers
    columnHeaders = ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT"]
    
    # get the initial fields
    vcfHeader = ""
    vcfHeader += "##fileformat=" + aVCFFormat + "\n"
    vcfHeader += "##tcgaversion=1.0\n"
    vcfHeader += "##fileDate=" + datetime.date.today().strftime("%Y%m%d") + "\n"
    vcfHeader += "##center=UCSC\n"
    if (aDisease != None):
        vcfHeader += "##disease=" + aDisease + "\n"
    vcfHeader += "##source=\"RADIA pipeline " + aRadiaVersion + "\"\n"
    
    # add the reference information to the header
    if (aRefId != None):
        if (aRefFilename != None):
            vcfHeader += "##reference=<ID=" + aRefId + ",Source=file:" + aRefFilename + ">\n"
        elif (aFastaFilename != None):
            vcfHeader += "##reference=<ID=" + aRefId + ",Source=file:" + aFastaFilename + ">\n"
    elif (aRefFilename != None):
        vcfHeader += "##reference=file:" + aRefFilename + "\n"
    else:
        vcfHeader += "##reference=file:" + aFastaFilename + "\n"
    
    # add the URL or the fasta to the assembly tag
    if (aRefURL != None):
        vcfHeader += "##assembly=" + aRefURL + "\n"
    else:
        vcfHeader += "##assembly=file:" + aFastaFilename + "\n"
        
    vcfHeader += "##phasing=none\n"
        
    # add RADIA param info
    aParamDict["algorithm"] = "RADIA"
    aParamDict["version"] = "1.1.5"
    #vcfHeader += "##vcfProcessLog=<"
    vcfHeader += "##vcfGenerator=<"
    for (paramName) in sorted(aParamDict.iterkeys()):
        paramValue = aParamDict[paramName]
        if (paramValue != None):
            # don't output the defaults for files that aren't specified
            if (paramName.startswith("dnaNormal") and "DNA_NORMAL" not in aLabelList):
                continue;
            elif (paramName.startswith("rnaNormal") and "RNA_NORMAL" not in aLabelList):
                continue;
            elif (paramName.startswith("dnaTumor") and "DNA_TUMOR" not in aLabelList):
                continue;
            elif (paramName.startswith("rnaTumor") and "RNA_TUMOR" not in aLabelList):
                continue;
            else:
                if (type(paramValue) is str and " " in paramValue):
                    vcfHeader += paramName + "=<\"" + str(paramValue) + "\">,"
                else:
                    vcfHeader += paramName + "=<" + str(paramValue) + ">,"
                    
    vcfHeader = vcfHeader.rstrip(",")
    vcfHeader += ">\n"
    
    vcfHeader += "##INDIVIDUAL=" + aPatientId + "\n"
    
    # get the sample fields
    for (filename, label, description, platform, source) in izip(aFilenameList, aLabelList, aDescList, aPlatformList, aSourceList):
        # try to determine the TCGA barcode from the filename for the header SampleName
        # i_tcgaNameRegEx = re.compile("TCGA-(\\w){2}-(\\w){4}-(\\w){3}-(\\w){3}")
        matchObj = i_tcgaNameRegEx.search(filename)
        if (matchObj != None):
            vcfHeader += "##SAMPLE=<ID=" + label + ",SampleName=" + matchObj.group() + ",Individual=" + aPatientId + ",Description=\"" + description + "\",File=\"" + filename + "\","
        else:
            vcfHeader += "##SAMPLE=<ID=" + label + ",SampleName=" + aPatientId + ",Individual=" + aPatientId + ",Description=\"" + description + "\",File=\"" + filename + "\","
        
        if (platform != None):
            vcfHeader += "Platform=\"" + platform + "\","
        if (source != None):
            vcfHeader += "Source=\"" + source + "\","
            
        vcfHeader = vcfHeader.rstrip(",")
        vcfHeader += ">\n" 
        columnHeaders.append(label)

    # get the info fields
    vcfHeader += "##INFO=<ID=NS,Number=1,Type=Integer,Description=\"Number of samples with data\">\n"
    vcfHeader += "##INFO=<ID=AN,Number=1,Type=Integer,Description=\"Number of unique alleles across all samples\">\n"
    vcfHeader += "##INFO=<ID=AC,Number=.,Type=Integer,Description=\"Allele count in genotypes, for each ALT allele, in the same order as listed\">\n" 
    vcfHeader += "##INFO=<ID=AF,Number=.,Type=Float,Description=\"Allele frequency, for each ALT allele, in the same order as listed\">\n" 
    vcfHeader += "##INFO=<ID=DP,Number=1,Type=Integer,Description=\"Total read depth for all samples\">\n"
    vcfHeader += "##INFO=<ID=INDEL,Number=1,Type=Integer,Description=\"Number of indels for all samples\">\n"
    vcfHeader += "##INFO=<ID=START,Number=1,Type=Integer,Description=\"Number of reads starting at this position across all samples\">\n"
    vcfHeader += "##INFO=<ID=STOP,Number=1,Type=Integer,Description=\"Number of reads stopping at this position across all samples\">\n"
    vcfHeader += "##INFO=<ID=BQ,Number=1,Type=Float,Description=\"Overall average base quality\">\n"
    #vcfHeader += "##INFO=<ID=MQ,Number=1,Type=Float,Description=\"Overall average mapping quality\">\n"
    vcfHeader += "##INFO=<ID=SB,Number=1,Type=Float,Description=\"Overall strand bias\">\n"
    vcfHeader += "##INFO=<ID=FA,Number=1,Type=Float,Description=\"Overall fraction of reads supporting ALT\">\n"
    vcfHeader += "##INFO=<ID=MT,Number=.,Type=String,Description=\"Modification types at this position\">\n"
    vcfHeader += "##INFO=<ID=MC,Number=.,Type=String,Description=\"Modification base changes at this position\">\n"
    vcfHeader += "##INFO=<ID=SOMATIC,Number=0,Type=Flag,Description=\"Indicates if record is a somatic mutation\">\n"
    vcfHeader += "##INFO=<ID=VT,Number=1,Type=String,Description=\"Variant type, can be SNP, INS or DEL\">\n" 
    #vcfHeader += "##INFO=<ID=DEL,Number=1,Type=Integer,Description=\"Number of deletions in all samples at this position\">\n"
    #vcfHeader += "##INFO=<ID=INS,Number=1,Type=Integer,Description=\"Number of insertions in all samples at this position\">\n"
    #vcfHeader += "##INFO=<ID=VC,Number=1,Type=String,Description=\"Somatic variant classification (Intergenic, DEL, INS)\">\n"

    # get the filter fields
    vcfHeader += "##FILTER=<ID=noref,Description=\"Position skipped, reference=N\">\n"
    vcfHeader += "##FILTER=<ID=diffref,Description=\"Position skipped, different references in files\">\n"
    vcfHeader += "##FILTER=<ID=mbt,Description=\"Minimum total bases is less than user-specified cut-off\">\n"
    vcfHeader += "##FILTER=<ID=mba,Description=\"Minimum ALT bases is less than user-specified cut-off\">\n"
    
    # get the format fields
    # these fields are sample specific
    vcfHeader += "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
    vcfHeader += "##FORMAT=<ID=DP,Number=1,Type=Integer,Description=\"Read depth at this position in the sample\">\n"
    vcfHeader += "##FORMAT=<ID=INDEL,Number=1,Type=Integer,Description=\"Number of indels\">\n"
    vcfHeader += "##FORMAT=<ID=START,Number=1,Type=Integer,Description=\"Number of reads starting at this position\">\n"
    vcfHeader += "##FORMAT=<ID=STOP,Number=1,Type=Integer,Description=\"Number of reads stopping at this position\">\n"
    vcfHeader += "##FORMAT=<ID=AD,Number=.,Type=Float,Description=\"Depth of reads supporting alleles\">\n"
    vcfHeader += "##FORMAT=<ID=AF,Number=.,Type=Float,Description=\"Fraction of reads supporting alleles\">\n"
    vcfHeader += "##FORMAT=<ID=BQ,Number=.,Type=Float,Description=\"Avg base quality for reads supporting alleles\">\n"
    #vcfHeader += "##FORMAT=<ID=MQ,Number=.,Type=Float,Description=\"Avg mapping quality for reads supporting alleles\">\n"
    vcfHeader += "##FORMAT=<ID=SB,Number=.,Type=Float,Description=\"Strand Bias for reads supporting alleles\">\n"
    vcfHeader += "##FORMAT=<ID=SS,Number=1,Type=Integer,Description=\"Variant status relative to non-adjacent Normal, 0=wildtype,1=germline,2=somatic,3=LOH,4=unknown,5=rnaEditing\">\n"
    vcfHeader += "##FORMAT=<ID=SSC,Number=1,Type=Integer,Description=\"Somatic score between 0 and 255\">\n"
    
    vcfHeader += "#" + "\t".join(columnHeaders)
    return vcfHeader
           
           
def pad(aList, aPadder, aLength):
    '''
    ' This function pads a list with the value specified in the aPadder variable to the length specified in the aLength variable.
    '
    ' aList - The list to be padded
    ' aPadder - The value to pad with
    ' aLength - The length of the final list after padding 
    '''
    return aList + [aPadder] * (aLength - len(aList))
           
        
def pad_output(anOutput, anAlleleLength):
    '''
    ' This function pads some of the output components with null or zero values.  If a variant is found in a sample and the output for 
    ' previous samples has already been formatted, it needs to be reformatted with null or zero values.  For example, all of the allele 
    ' specific components such as depth and frequency need to be set to zero in previous samples.
    '
    ' anOutput - The formatted output for a sample
    ' anAlleleLength - The number of alleles found at this site 
    '''
    
    #TODO: make this accept any format
    #format = "GT:DP:INDEL:START:STOP:AD:AF:BQ:SB"
    
    # if there is no data, then just return
    if (anOutput == "."):
        return anOutput
    
    # get the data for this sample
    (genotypes, depths, indels, starts, stops, alleleDepths, alleleFractions, baseQualities, strandBiases) = anOutput.split(":")
    
    # if we need some padding
    alleleDepthList = alleleDepths.split(",") 
    if (len(alleleDepthList) < anAlleleLength):
        alleleDepthList = pad(alleleDepthList, "0", anAlleleLength)
        alleleFractionList = pad(alleleFractions.split(","), "0.0", anAlleleLength)
        baseQualityList = pad(baseQualities.split(","), "0.0", anAlleleLength)
        strandBiasList = pad(strandBiases.split(","), "0.0", anAlleleLength)
        
        outputList = (genotypes, depths, indels, starts, stops, ",".join(alleleDepthList), ",".join(alleleFractionList), ",".join(baseQualityList), ",".join(strandBiasList))
            
        return ":".join(outputList)
    else:
    # no padding necessary
        return anOutput
                    
               
def main():
    
    # command for running this on a small test case: 
    #python radia.py TCGA-AB-2995 12 --normalUseChr --tumorUseChr --rnaUseChr -n ../data/test/TCGA-AB-2995_normal.sam -t ../data/test/TCGA-AB-2995_tumor.sam -r ../data/test/TCGA-AB-2995_rna.sam
    
    # commands for running this on real data:    
    #python radia.py uniqueId X -n normalDna.bam -t tumorDna.bam -r tumorRna.bam -f all_sequences.fasta -o /path/to/output/uniqueId.vcf -e hg19 -u https://url/to/fasta/hg19.fasta
     
    i_radiaVersion = "v1.1.5"
    i_vcfFormat = "VCFv4.1"
    
    # create the usage statement
    usage = "usage: python %prog id chrom [Options]"
    i_cmdLineParser = OptionParser(usage=usage, version=i_radiaVersion)
    
    # add the optional parameters
    i_cmdLineParser.add_option("-b", "--batchSize", type="int", dest="batchSize", default=int(250000000), metavar="BATCH_SIZE", help="the size of the samtool selections that are loaded into memory at one time, %default by default")
    i_cmdLineParser.add_option("-o", "--outputFilename", dest="outputFilename", metavar="OUTPUT_FILE", help="the name of the output file, STDOUT by default")
    i_cmdLineParser.add_option("-c", "--chromSizesFilename", dest="chromSizesFilename", metavar="CHROM_SIZES_FILE", help="the name of the file with the chromosome sizes")
    i_cmdLineParser.add_option("-f", "--fastaFilename", dest="fastaFilename", metavar="FASTA_FILE", help="the name of the fasta file that can be used on all .bams, see below for specifying individual fasta files for each .bam file")
    i_cmdLineParser.add_option("-p", "--useChrPrefix", action="store_true", default=False, dest="useChrPrefix", help="include this argument if the 'chr' prefix should be used in the samtools command for all .bams, see below for specifying the prefix for individual .bam files")
    i_cmdLineParser.add_option("-l", "--log", dest="logLevel", default="WARNING", metavar="LOG", help="the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL), %default by default")
    i_cmdLineParser.add_option("-g", "--logFilename", dest="logFilename", metavar="LOG_FILE", help="the name of the log file, STDOUT by default")
    i_cmdLineParser.add_option("-i", "--refId", dest="refId", metavar="REF_ID", help="the reference Id - used in the reference VCF meta tag")
    i_cmdLineParser.add_option("-u", "--refUrl", dest="refUrl", metavar="REF_URL", help="the URL for the reference - used in the reference VCF meta tag")
    i_cmdLineParser.add_option("-m", "--refFilename", dest="refFilename", metavar="REF_FILE", help="the location of the reference - used in the reference VCF meta tag")
    i_cmdLineParser.add_option("-a", "--startCoordinate", type="int", default=int(1), dest="startCoordinate", metavar="START_COORDINATE", help="the start coordinate for testing small regions, %default by default")
    i_cmdLineParser.add_option("-z", "--stopCoordinate", type="int", default=int(0), dest="stopCoordinate", metavar="STOP_COORDINATE", help="the stop coordinate for testing small regions, %default by default")
    i_cmdLineParser.add_option("-d", "--dataSource", dest="dataSource", metavar="DATA_SOURCE", help="the source of the data - used in the sample VCF meta tag")
    i_cmdLineParser.add_option("-q", "--sequencingPlatform", dest="sequencingPlatform", metavar="SEQ_PLATFORM", help="the sequencing platform - used in the sample VCF meta tag")
    i_cmdLineParser.add_option("-s", "--statsDir", dest="statsDir", metavar="STATS_DIR", help="a stats directory where some basic stats can be output")
    i_cmdLineParser.add_option("", "--disease", dest="disease", metavar="DISEASE", help="a disease abbreviation (i.e. BRCA) for the header")
    #e,j,k,v,w,y
    
    i_cmdLineParser.add_option("", "--genotypeMinDepth", type="int", default=int(2), dest="genotypeMinDepth", metavar="GT_MIN_DP", help="the minimum number of bases required for the genotype, %default by default")
    i_cmdLineParser.add_option("", "--genotypeMinPct", type="float", default=float(.10), dest="genotypeMinPct", metavar="GT_MIN_PCT", help="the minimum percentage of reads required for the genotype, %default by default")
    
    # params for normal DNA
    i_cmdLineParser.add_option("-n", "--dnaNormalFilename", dest="dnaNormalFilename", metavar="DNA_NORMAL_FILE", help="the name of the normal DNA .bam file")
    i_cmdLineParser.add_option("", "--dnaNormalMinTotalBases", type="int", default=int(4), dest="dnaNormalMinTotalNumBases", metavar="DNA_NOR_MIN_TOTAL_BASES", help="the minimum number of overall normal DNA reads covering a position, %default by default")
    i_cmdLineParser.add_option("", "--dnaNormalMinAltBases", type="int", default=int(2), dest="dnaNormalMinAltNumBases", metavar="DNA_NOR_MIN_ALT_BASES", help="the minimum number of alternative normal DNA reads supporting a variant at a position, %default by default")
    i_cmdLineParser.add_option("", "--dnaNormalBaseQual", type="int", default=int(10), dest="dnaNormalMinBaseQuality", metavar="DNA_NOR_BASE_QUAL", help="the minimum normal DNA base quality, %default by default")
    i_cmdLineParser.add_option("", "--dnaNormalMapQual", type="int", default=int(10), dest="dnaNormalMinMappingQuality", metavar="DNA_NOR_MAP_QUAL", help="the minimum normal DNA mapping quality, %default by default")
    i_cmdLineParser.add_option("", "--dnaNormalUseChr", action="store_true", default=False, dest="dnaNormalUseChrPrefix", help="include this argument if the 'chr' prefix should be used in the samtools command for the normal DNA .bam file")
    i_cmdLineParser.add_option("", "--dnaNormalFasta", dest="dnaNormalFastaFilename", metavar="DNA_NOR_FASTA_FILE", help="the name of the fasta file for the normal DNA .bam file")
    i_cmdLineParser.add_option("", "--dnaNormalMitochon", default = "M", dest="dnaNormalMitochon", metavar="DNA_NOR_MITOCHON", help="the short name for the mitochondrial DNA (e.g 'M' or 'MT'), %default by default")
    i_cmdLineParser.add_option("", "--dnaNormalDescription", default = "Normal DNA Sample", dest="dnaNormalDesc", metavar="DNA_NOR_DESC", help="the description for the sample in the VCF header, %default by default")
    #i_cmdLineParser.add_option("", "--dnaNormalLabel", default = "DNA_NORMAL", dest="dnaNormalLabel", metavar="DNA_NOR_LABEL", help="the column header for the sample in the VCF file, %default by default")
    
    # params for normal RNA
    i_cmdLineParser.add_option("-x", "--rnaNormalFilename", dest="rnaNormalFilename", metavar="RNA_NORMAL_FILE", help="the name of the normal RNA-Seq .bam file")
    i_cmdLineParser.add_option("", "--rnaNormalMinTotalBases", type="int", default=int(4), dest="rnaNormalMinTotalNumBases", metavar="RNA_NOR_MIN_TOTAL_BASES", help="the minimum number of overall normal RNA-Seq reads covering a position, %default by default")
    i_cmdLineParser.add_option("", "--rnaNormalMinAltBases", type="int", default=int(2), dest="rnaNormalMinAltNumBases", metavar="RNA_NOR_MIN_ALT_BASES", help="the minimum number of alternative normal RNA-Seq reads supporting a variant at a position, %default by default")
    i_cmdLineParser.add_option("", "--rnaNormalBaseQual", type="int", default=int(10), dest="rnaNormalMinBaseQuality", metavar="RNA_NOR_BASE_QUAL", help="the minimum normal RNA-Seq base quality, %default by default")
    i_cmdLineParser.add_option("", "--rnaNormalMapQual", type="int", default=int(10), dest="rnaNormalMinMappingQuality", metavar="RNA_NOR_MAP_QUAL", help="the minimum normal RNA-Seq mapping quality, %default by default")
    i_cmdLineParser.add_option("", "--rnaNormalUseChr", action="store_true", default=False, dest="rnaNormalUseChrPrefix", help="include this argument if the 'chr' prefix should be used in the samtools command for the normal RNA .bam file")
    i_cmdLineParser.add_option("", "--rnaNormalFasta", dest="rnaNormalFastaFilename", metavar="RNA_NOR_FASTA_FILE", help="the name of the fasta file for the normal RNA .bam file")    
    i_cmdLineParser.add_option("", "--rnaNormalMitochon", default = "M", dest="rnaNormalMitochon", metavar="RNA_NOR_MITOCHON", help="the short name for the mitochondrial RNA (e.g 'M' or 'MT'), %default by default")
    i_cmdLineParser.add_option("", "--rnaNormalDescription", default = "Normal RNA Sample", dest="rnaNormalDesc", metavar="RNA_NOR_DESC", help="the description for the sample in the VCF header, %default by default")
    #i_cmdLineParser.add_option("", "--rnaNormalLabel", default = "RNA_NORMAL", dest="rnaNormalLabel", metavar="RNA_NOR_LABEL", help="the column header for the sample in the VCF file, %default by default")
    
    # params for tumor DNA
    i_cmdLineParser.add_option("-t", "--dnaTumorFilename", dest="dnaTumorFilename", metavar="DNA_TUMOR_FILE", help="the name of the tumor DNA .bam file")
    i_cmdLineParser.add_option("", "--dnaTumorMinTotalBases", type="int", default=int(4), dest="dnaTumorMinTotalNumBases", metavar="DNA_TUM_MIN_TOTAL_BASES", help="the minimum number of overall tumor DNA reads covering a position, %default by default")
    i_cmdLineParser.add_option("", "--dnaTumorMinAltBases", type="int", default=int(2), dest="dnaTumorMinAltNumBases", metavar="DNA_TUM_MIN_ALT_BASES", help="the minimum number of alternative tumor DNA reads supporting a variant at a position, %default by default")
    i_cmdLineParser.add_option("", "--dnaTumorBaseQual", type="int", default=int(10), dest="dnaTumorMinBaseQuality", metavar="DNA_TUM_BASE_QUAL", help="the minimum tumor DNA base quality, %default by default")
    i_cmdLineParser.add_option("", "--dnaTumorMapQual", type="int", default=int(10), dest="dnaTumorMinMappingQuality", metavar="DNA_TUM_MAP_QUAL", help="the minimum tumor DNA mapping quality, %default by default")
    i_cmdLineParser.add_option("", "--dnaTumorUseChr", action="store_true", default=False, dest="dnaTumorUseChrPrefix", help="include this argument if the 'chr' prefix should be used in the samtools command for the tumor DNA .bam file")
    i_cmdLineParser.add_option("", "--dnaTumorFasta", dest="dnaTumorFastaFilename", metavar="DNA_TUM_FASTA_FILE", help="the name of the fasta file for the tumor DNA .bam file")
    i_cmdLineParser.add_option("", "--dnaTumorMitochon", default = "M", dest="dnaTumorMitochon", metavar="DNA_TUM_MITOCHON", help="the short name for the mitochondrial DNA (e.g 'M' or 'MT'), %default by default")
    i_cmdLineParser.add_option("", "--dnaTumorDescription", default = "Tumor DNA Sample", dest="dnaTumorDesc", metavar="DNA_TUM_DESC", help="the description for the sample in the VCF header, %default by default")
    #i_cmdLineParser.add_option("", "--dnaTumorLabel", default = "DNA_TUMOR", dest="dnaTumorLabel", metavar="DNA_TUM_LABEL", help="the column header for the sample in the VCF file, %default by default")
    
    # params for tumor RNA
    i_cmdLineParser.add_option("-r", "--rnaTumorFilename", dest="rnaTumorFilename", metavar="RNA_TUMOR_FILE", help="the name of the tumor RNA-Seq .bam file")
    i_cmdLineParser.add_option("", "--rnaTumorMinTotalBases", type="int", default=int(4), dest="rnaTumorMinTotalNumBases", metavar="RNA_TUM_MIN_TOTAL_BASES", help="the minimum number of overall tumor RNA-Seq reads covering a position, %default by default")
    i_cmdLineParser.add_option("", "--rnaTumorMinAltBases", type="int", default=int(2), dest="rnaTumorMinAltNumBases", metavar="RNA_TUM_MIN_ALT_BASES", help="the minimum number of alternative tumor RNA-Seq reads supporting a variant at a position, %default by default")
    i_cmdLineParser.add_option("", "--rnaTumorBaseQual", type="int", default=int(10), dest="rnaTumorMinBaseQuality", metavar="RNA_TUM_BASE_QUAL", help="the minimum tumor RNA-Seq base quality, %default by default")
    i_cmdLineParser.add_option("", "--rnaTumorMapQual", type="int", default=int(10), dest="rnaTumorMinMappingQuality", metavar="RNA_TUM_MAP_QUAL", help="the minimum tumor RNA-Seq mapping quality, %default by default")
    i_cmdLineParser.add_option("", "--rnaTumorUseChr", action="store_true", default=False, dest="rnaTumorUseChrPrefix", help="include this argument if the 'chr' prefix should be used in the samtools command for the tumor RNA .bam file")
    i_cmdLineParser.add_option("", "--rnaTumorFasta", dest="rnaTumorFastaFilename", metavar="RNA_TUM_FASTA_FILE", help="the name of the fasta file for the tumor RNA .bam file")    
    i_cmdLineParser.add_option("", "--rnaTumorMitochon", default = "M", dest="rnaTumorMitochon", metavar="RNA_TUM_MITOCHON", help="the short name for the mitochondrial RNA (e.g 'M' or 'MT'), %default by default")
    i_cmdLineParser.add_option("", "--rnaTumorDescription", default = "Tumor RNA Sample", dest="rnaTumorDesc", metavar="RNA_TUM_DESC", help="the description for the sample in the VCF header, %default by default")
    #i_cmdLineParser.add_option("", "--rnaTumorLabel", default = "RNA_TUMOR", dest="rnaTumorLabel", metavar="RNA_TUM_LABEL", help="the column header for the sample in the VCF file, %default by default")
    
    # first parse the args
    (i_cmdLineOptions, i_cmdLineArgs) = i_cmdLineParser.parse_args()
    
    # range(inclusiveFrom, exclusiveTo, by)
    i_possibleArgLengths = range(3,80,1)
    i_argLength = len(sys.argv)
    
    # check if this is one of the possible correct commands
    if (i_argLength not in i_possibleArgLengths):   
        i_cmdLineParser.print_help()
        sys.exit(0)
    
    # get the required params
    i_cmdLineOptionsDict = vars(i_cmdLineOptions)
    i_id = str(i_cmdLineArgs[0])
    i_chrom = str(i_cmdLineArgs[1])
    
    # get the optional params with default values
    i_batchSize = i_cmdLineOptions.batchSize
    i_useChrPrefix = i_cmdLineOptions.useChrPrefix
    i_logLevel = i_cmdLineOptions.logLevel 
    i_startCoordinate = i_cmdLineOptions.startCoordinate
    i_stopCoordinate = i_cmdLineOptions.stopCoordinate
    i_refId = i_cmdLineOptions.refId
    i_refUrl = i_cmdLineOptions.refUrl
    
    i_genotypeMinDepth = i_cmdLineOptions.genotypeMinDepth
    i_genotypeMinPct = i_cmdLineOptions.genotypeMinPct
        
    i_dnaNormMinTotalNumBases = i_cmdLineOptions.dnaNormalMinTotalNumBases
    i_dnaNormMinAltNumBases = i_cmdLineOptions.dnaNormalMinAltNumBases
    i_dnaNormBaseQual = i_cmdLineOptions.dnaNormalMinBaseQuality
    i_dnaNormMapQual = i_cmdLineOptions.dnaNormalMinMappingQuality
    i_dnaNormUseChr = i_cmdLineOptions.dnaNormalUseChrPrefix
    i_dnaNormMitochon = i_cmdLineOptions.dnaNormalMitochon
    i_dnaNormDesc = i_cmdLineOptions.dnaNormalDesc
    #i_dnaNormLabel = i_cmdLineOptions.dnaNormalLabel
    i_dnaNormLabel = "DNA_NORMAL"
    
    i_rnaNormMinTotalNumBases = i_cmdLineOptions.rnaNormalMinTotalNumBases
    i_rnaNormMinAltNumBases = i_cmdLineOptions.rnaNormalMinAltNumBases
    i_rnaNormBaseQual = i_cmdLineOptions.rnaNormalMinBaseQuality
    i_rnaNormMapQual = i_cmdLineOptions.rnaNormalMinMappingQuality
    i_rnaNormUseChr = i_cmdLineOptions.rnaNormalUseChrPrefix
    i_rnaNormMitochon = i_cmdLineOptions.rnaNormalMitochon
    i_rnaNormDesc = i_cmdLineOptions.rnaNormalDesc
    #i_rnaNormLabel = i_cmdLineOptions.rnaNormalLabel
    i_rnaNormLabel = "RNA_NORMAL"
    
    i_dnaTumMinTotalNumBases = i_cmdLineOptions.dnaTumorMinTotalNumBases
    i_dnaTumMinAltNumBases = i_cmdLineOptions.dnaTumorMinAltNumBases
    i_dnaTumBaseQual = i_cmdLineOptions.dnaTumorMinBaseQuality
    i_dnaTumMapQual = i_cmdLineOptions.dnaTumorMinMappingQuality
    i_dnaTumUseChr = i_cmdLineOptions.dnaTumorUseChrPrefix
    i_dnaTumMitochon = i_cmdLineOptions.dnaTumorMitochon
    i_dnaTumDesc = i_cmdLineOptions.dnaTumorDesc
    #i_dnaTumLabel = i_cmdLineOptions.dnaTumorLabel
    i_dnaTumLabel = "DNA_TUMOR"
    
    i_rnaTumMinTotalNumBases = i_cmdLineOptions.rnaTumorMinTotalNumBases
    i_rnaTumMinAltNumBases = i_cmdLineOptions.rnaTumorMinAltNumBases
    i_rnaTumBaseQual = i_cmdLineOptions.rnaTumorMinBaseQuality
    i_rnaTumMapQual = i_cmdLineOptions.rnaTumorMinMappingQuality
    i_rnaTumUseChr = i_cmdLineOptions.rnaTumorUseChrPrefix
    i_rnaTumMitochon = i_cmdLineOptions.rnaTumorMitochon
    i_rnaTumDesc = i_cmdLineOptions.rnaTumorDesc
    #i_rnaTumLabel = i_cmdLineOptions.rnaTumorLabel
    i_rnaTumLabel = "RNA_TUMOR"
        
    # the user can specify that the prefix should be used on all bams with one param
    if (i_useChrPrefix):
        i_dnaNormUseChr = True
        i_dnaTumUseChr = True
        i_rnaNormUseChr = True
        i_rnaTumUseChr = True
    
    # try to get any optional parameters with no defaults    
    i_readFilenameList = []
    i_writeFilenameList = []
    i_dirList = []
    filenames = []
    labels = []
    descriptions = []
    
    i_outputFilename = None
    i_logFilename = None
    i_dnaNormalFilename = None
    i_dnaNormalGenerator = None
    i_dnaTumorFilename = None
    i_dnaTumorGenerator = None
    i_rnaNormalFilename = None
    i_rnaNormalGenerator = None
    i_rnaTumorFilename = None
    i_rnaTumorGenerator = None
    i_dnaNormalFastaFilename = None
    i_dnaTumorFastaFilename = None
    i_rnaNormalFastaFilename = None
    i_rnaTumorFastaFilename = None
    i_chromSizesFilename = None
    i_universalFastaFilename = None
    i_refFilename = None
    i_statsDir = None
    i_dataSource = None
    i_sequencingPlatform = None
    i_disease = None
    
    if (i_cmdLineOptions.dnaNormalFilename != None):
        i_dnaNormalFilename = str(i_cmdLineOptions.dnaNormalFilename)
        i_readFilenameList += [i_dnaNormalFilename]   
        filenames += [i_dnaNormalFilename]
        labels += [i_dnaNormLabel]
        descriptions += [i_dnaNormDesc]
    if (i_cmdLineOptions.rnaNormalFilename != None):
        i_rnaNormalFilename = str(i_cmdLineOptions.rnaNormalFilename)
        i_readFilenameList += [i_rnaNormalFilename] 
        filenames += [i_rnaNormalFilename] 
        labels += [i_rnaNormLabel]
        descriptions += [i_rnaNormDesc]
    if (i_cmdLineOptions.dnaTumorFilename != None):
        i_dnaTumorFilename = str(i_cmdLineOptions.dnaTumorFilename)
        i_readFilenameList += [i_dnaTumorFilename] 
        filenames += [i_dnaTumorFilename]  
        labels += [i_dnaTumLabel]
        descriptions += [i_dnaTumDesc]
    if (i_cmdLineOptions.rnaTumorFilename != None):
        i_rnaTumorFilename = str(i_cmdLineOptions.rnaTumorFilename)
        i_readFilenameList += [i_rnaTumorFilename]  
        filenames += [i_rnaTumorFilename]
        labels += [i_rnaTumLabel]
        descriptions += [i_rnaTumDesc]
    if (i_cmdLineOptions.outputFilename != None):
        i_outputFilename = str(i_cmdLineOptions.outputFilename)
        i_writeFilenameList += [i_outputFilename]
    if (i_cmdLineOptions.logFilename != None):
        i_logFilename = str(i_cmdLineOptions.logFilename)
        i_writeFilenameList += [i_logFilename]
    if (i_cmdLineOptions.chromSizesFilename != None):
        i_chromSizesFilename = str(i_cmdLineOptions.chromSizesFilename)
        i_readFilenameList += [i_chromSizesFilename]
    if (i_cmdLineOptions.refFilename != None):
        i_refFilename = str(i_cmdLineOptions.refFilename)
    if (i_cmdLineOptions.statsDir != None):
        i_statsDir = str(i_cmdLineOptions.statsDir)
        i_dirList += [i_statsDir]
    if (i_cmdLineOptions.dataSource != None):
        i_dataSource = str(i_cmdLineOptions.dataSource)
    if (i_cmdLineOptions.sequencingPlatform != None):
        i_sequencingPlatform = str(i_cmdLineOptions.sequencingPlatform)
    if (i_cmdLineOptions.disease != None):
        i_disease = str(i_cmdLineOptions.disease)
    
    # if a universal fasta file is specified, then use it
    if (i_cmdLineOptions.fastaFilename != None):
        i_universalFastaFilename = str(i_cmdLineOptions.fastaFilename)
        i_dnaNormalFastaFilename = i_universalFastaFilename
        i_dnaTumorFastaFilename = i_universalFastaFilename
        i_rnaNormalFastaFilename = i_universalFastaFilename
        i_rnaTumorFastaFilename = i_universalFastaFilename
        
    # if individual fasta files are specified, they over-ride the universal one
    if (i_cmdLineOptions.dnaNormalFastaFilename != None):
        i_dnaNormalFastaFilename = str(i_cmdLineOptions.dnaNormalFastaFilename)
        i_readFilenameList += [i_dnaNormalFastaFilename]
        if (i_universalFastaFilename == None):
            i_universalFastaFilename = i_dnaNormalFastaFilename
    if (i_cmdLineOptions.rnaNormalFastaFilename != None):
        i_rnaNormalFastaFilename = str(i_cmdLineOptions.rnaNormalFastaFilename)
        i_readFilenameList += [i_rnaNormalFastaFilename]
        if (i_universalFastaFilename == None):
            i_universalFastaFilename = i_rnaNormalFastaFilename
    if (i_cmdLineOptions.dnaTumorFastaFilename != None):
        i_dnaTumorFastaFilename = str(i_cmdLineOptions.dnaTumorFastaFilename)
        i_readFilenameList += [i_dnaTumorFastaFilename]
        if (i_universalFastaFilename == None):
            i_universalFastaFilename = i_dnaTumorFastaFilename
    if (i_cmdLineOptions.rnaTumorFastaFilename != None):
        i_rnaTumorFastaFilename = str(i_cmdLineOptions.rnaTumorFastaFilename)
        i_readFilenameList += [i_rnaTumorFastaFilename]
        if (i_universalFastaFilename == None):
            i_universalFastaFilename = i_rnaTumorFastaFilename
    i_readFilenameList += [i_universalFastaFilename]
        
    # need to set these for the vcf header, especially when only a universal fasta file is specified
    i_cmdLineOptionsDict["dnaNormalFastaFilename"] = i_dnaNormalFastaFilename
    i_cmdLineOptionsDict["dnaTumorFastaFilename"] = i_dnaTumorFastaFilename
    i_cmdLineOptionsDict["rnaNormalFastaFilename"] = i_rnaNormalFastaFilename
    i_cmdLineOptionsDict["rnaTumorFastaFilename"] = i_rnaTumorFastaFilename
        
    # assuming loglevel is bound to the string value obtained from the
    # command line argument. Convert to upper case to allow the user to
    # specify --log=DEBUG or --log=debug
    i_numericLogLevel = getattr(logging, i_logLevel.upper(), None)
    if not isinstance(i_numericLogLevel, int):
        raise ValueError("Invalid log level: '%s' must be one of the following:  DEBUG, INFO, WARNING, ERROR, CRITICAL", i_logLevel)
    
    # set up the logging
    if (i_logFilename != None):
        logging.basicConfig(level=i_numericLogLevel, filename=i_logFilename, filemode='w', format='%(asctime)s\t%(levelname)s\t%(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
    else:
        logging.basicConfig(level=i_numericLogLevel, format='%(asctime)s\t%(levelname)s\t%(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
    
    # set the debug    
    #i_debug = (i_numericLogLevel < logging.WARNING)
    i_debug = (i_numericLogLevel == logging.DEBUG)
        
    # output some debug info
    if (i_debug):
        logging.debug("id=%s" % i_id)
        logging.debug("chrom=%s" % i_chrom)
        logging.debug("outputFilename=%s" % i_outputFilename)
        logging.debug("logLevel=%s" % i_logLevel)
        logging.debug("logFilename=%s" % i_logFilename)
        logging.debug("batchSize=%s" % i_batchSize)
        logging.debug("chromSizeFile=%s" % i_chromSizesFilename)
        logging.debug("vcfFormat=%s" % i_vcfFormat)
        logging.debug("startCoordinate=%s" % i_startCoordinate)
        logging.debug("stopCoordinate=%s" % i_stopCoordinate)
        logging.debug("refId=%s" % i_refId)
        logging.debug("refUrl=%s" % i_refUrl)
        logging.debug("disease=%s" % i_disease)
        logging.debug("refFilename=%s" % i_refFilename)
        logging.debug("statsDir=%s" % i_statsDir)
        
        logging.debug("genotypeMinDepth=%s" % i_genotypeMinDepth)
        logging.debug("genotypeMinPct=%s" % i_genotypeMinPct)
        
        if (i_dnaNormalFilename != None):
            logging.debug("dnaNormal=%s" % i_dnaNormalFilename)
        logging.debug("dna normal fasta File: %s" % i_dnaNormalFastaFilename)
        logging.debug("dna normal baseQual: %s" % i_dnaNormBaseQual)
        logging.debug("dna normal mappingQual: %s" % i_dnaNormMapQual)
        logging.debug("dna normal minTotalBases: %s" % i_dnaNormMinTotalNumBases)
        logging.debug("dna normal minAltBases: %s" % i_dnaNormMinAltNumBases)
        logging.debug("dna normal usePrefix? %s" % i_dnaNormUseChr)
        logging.debug("dna normal mitochon %s" % i_dnaNormMitochon)
    
        if (i_dnaTumorFilename != None):
            logging.debug("dnaTumor=%s" % i_dnaTumorFilename)
        logging.debug("dna tumor fasta File: %s" % i_dnaTumorFastaFilename)
        logging.debug("dna tumor baseQual: %s" % i_dnaTumBaseQual)
        logging.debug("dna tumor mappingQual: %s" % i_dnaTumMapQual)
        logging.debug("dna tumor minTotalBases: %s" % i_dnaTumMinTotalNumBases)
        logging.debug("dna tumor minAltBases: %s" % i_dnaTumMinAltNumBases)
        logging.debug("dna tumor usePrefix? %s" % i_dnaTumUseChr)
        logging.debug("dna tumor mitochon %s" % i_dnaTumMitochon)
        
        if (i_rnaNormalFilename != None):
            logging.debug("rnaNormal=%s" % i_rnaNormalFilename)
        logging.debug("rna normal fasta File: %s" % i_rnaNormalFastaFilename)
        logging.debug("rna normal baseQual: %s" % i_rnaNormBaseQual)
        logging.debug("rna normal mappingQual: %s" % i_rnaNormMapQual)
        logging.debug("rna normal minTotalBases: %s" % i_rnaNormMinTotalNumBases)
        logging.debug("rna normal minAltBases: %s" % i_rnaNormMinAltNumBases)
        logging.debug("rna normal usePrefix? %s" % i_rnaNormUseChr)
        logging.debug("rna normal mitochon %s" % i_rnaNormMitochon)
        
        if (i_rnaTumorFilename != None):
            logging.debug("rnaTumor=%s" % i_rnaTumorFilename)
        logging.debug("rna tumor fasta File: %s" % i_rnaTumorFastaFilename)
        logging.debug("rna tumor baseQual: %s" % i_rnaTumBaseQual)
        logging.debug("rna tumor mappingQual: %s" % i_rnaTumMapQual)
        logging.debug("rna tumor minTotalBases: %s" % i_rnaTumMinTotalNumBases)
        logging.debug("rna tumor minAltBases: %s" % i_rnaTumMinAltNumBases)
        logging.debug("rna tumor usePrefix? %s" % i_rnaTumUseChr)
        logging.debug("rna tumor mitochon %s" % i_rnaTumMitochon)
                    
    # check for any errors
    if (not radiaUtil.check_for_argv_errors(i_dirList, i_readFilenameList, i_writeFilenameList)):
        sys.exit(1)
        
    # the user must specify at least one .bam file
    if (i_dnaNormalFilename == None and i_dnaTumorFilename == None and i_rnaNormalFilename == None and i_rnaTumorFilename == None):
        logging.critical("You must specify at least one .bam file.")
        sys.exit(1)
    if (i_dnaNormalFilename != None and not os.path.isfile(i_dnaNormalFilename + ".bai")):
        logging.critical("The index file for the BAM file " + i_dnaNormalFilename + " doesn't exist.  Please use the 'samtools index' command to create one.")
        sys.exit(1)
    if (i_rnaNormalFilename != None and not os.path.isfile(i_rnaNormalFilename + ".bai")):
        logging.critical("The index file for the BAM file " + i_rnaNormalFilename + " doesn't exist.  Please use the 'samtools index' command to create one.")
        sys.exit(1)
    if (i_dnaTumorFilename != None and not os.path.isfile(i_dnaTumorFilename + ".bai")):
        logging.critical("The index file for the BAM file " + i_dnaTumorFilename + " doesn't exist.  Please use the 'samtools index' command to create one.")
        sys.exit(1)
    if (i_rnaTumorFilename != None and not os.path.isfile(i_rnaTumorFilename + ".bai")):
        logging.critical("The index file for the BAM file " + i_rnaTumorFilename + " doesn't exist.  Please use the 'samtools index' command to create one.")
        sys.exit(1)
        
    # make sure the user specified the necessary files
    if ((i_dnaNormalFilename != None and i_dnaNormalFastaFilename == None) or 
        (i_dnaTumorFilename != None and i_dnaTumorFastaFilename == None) or 
        (i_rnaNormalFilename != None and i_rnaNormalFastaFilename == None) or 
        (i_rnaTumorFilename != None and i_rnaTumorFastaFilename == None)):
        logging.critical("You must specify the appropriate FASTA files when running RADIA.")
        sys.exit(1)
    if (i_dnaNormalFilename != None and  i_dnaNormalFastaFilename != None and not os.path.isfile(i_dnaNormalFastaFilename + ".fai")):
        logging.critical("The index file for the FASTA file " + i_dnaNormalFastaFilename + " doesn't exist.  Please use the 'samtools faidx' command to create one.")
        sys.exit(1)
    if (i_rnaNormalFilename != None and i_rnaNormalFastaFilename != None and not os.path.isfile(i_rnaNormalFastaFilename + ".fai")):
        logging.critical("The index file for the FASTA file " + i_rnaNormalFastaFilename + " doesn't exist.  Please use the 'samtools faidx' command to create one.")
        sys.exit(1)
    if (i_dnaTumorFilename != None and i_dnaTumorFastaFilename != None and not os.path.isfile(i_dnaTumorFastaFilename + ".fai")):
        logging.critical("The index file for the FASTA file " + i_dnaTumorFastaFilename + " doesn't exist.  Please use the 'samtools faidx' command to create one.")
        sys.exit(1)
    if (i_rnaTumorFilename != None and i_rnaTumorFastaFilename != None and not os.path.isfile(i_rnaTumorFastaFilename + ".fai")):
        logging.critical("The index file for the FASTA file " + i_rnaTumorFastaFilename + " doesn't exist.  Please use the 'samtools faidx' command to create one.")
        sys.exit(1)
   
        
    # get the stop coordinate if it hasn't been specified
    if (i_stopCoordinate == 0):
        if (i_universalFastaFilename == None and i_chromSizesFilename == None):
            logging.critical("You must specify either a FASTA file or a tab separated file containing the chromosome sizes.")
            sys.exit(1)
        if (i_universalFastaFilename != None):
            i_chromSizeFileHandler = open(i_universalFastaFilename + ".fai", "r")
        else: 
            i_chromSizeFileHandler = get_read_fileHandler(i_chromSizesFilename)
        i_stopCoordinate = get_chrom_size(i_chrom, i_chromSizeFileHandler, i_debug)
        i_chromSizeFileHandler.close()
        
    # catch some errors on the selection coordinates
    if (i_stopCoordinate == -1):
        logging.critical("Couldn't find chromosome '%s' in the FASTA file that was specified.", i_chrom)
        sys.exit(1)
    if (i_startCoordinate >= i_stopCoordinate):
        logging.critical("The start coordinate must be less than the stop coordinate %s:%s-%s", i_chrom, i_startCoordinate, i_stopCoordinate)
        sys.exit(1)
        
    # EGFR chr7:55,248,979-55,259,567
    #i_startCoordinate = 55248979
    #i_stopCoordinate =  55249079
    #i_batchSize = 5
    
    if (i_debug):
        logging.debug("original i_startCoordinate: %s" % i_startCoordinate)
        logging.debug("original i_stopCoordinate: %s" % i_stopCoordinate)
        logging.debug("original i_batchSize: %s" % i_batchSize)
            
    # get the generators that will yield the pileups
    # Note:  Use the "get_sam_data" when testing locally on a .sam file 
    #        Use the "get_bam_data" when running on real .bam file data
    if ((i_dnaNormalFilename != None and i_dnaNormalFilename.endswith(".sam"))) :
        i_dnaNormalGenerator = get_sam_data(i_dnaNormalFilename, i_chrom, i_startCoordinate, i_stopCoordinate, i_dnaNormLabel, i_debug)
        
    if ((i_dnaTumorFilename != None and i_dnaTumorFilename.endswith(".sam"))):
        i_dnaTumorGenerator = get_sam_data(i_dnaTumorFilename, i_chrom, i_startCoordinate, i_stopCoordinate, i_dnaTumLabel, i_debug)
        
    if ((i_rnaNormalFilename != None and i_rnaNormalFilename.endswith(".sam"))):
        i_rnaNormalGenerator = get_sam_data(i_rnaNormalFilename, i_chrom, i_startCoordinate, i_stopCoordinate, i_rnaNormLabel, i_debug)
    
    if ((i_rnaTumorFilename != None and i_rnaTumorFilename.endswith(".sam"))):
        i_rnaTumorGenerator = get_sam_data(i_rnaTumorFilename, i_chrom, i_startCoordinate, i_stopCoordinate, i_rnaTumLabel, i_debug)
                     
                    
    if (i_dnaNormalFilename != None):
        # some bams/references use "M", some use "MT"
        if (i_chrom == "M" or i_chrom == "MT" and i_dnaNormMitochon != None):
            i_dnaNormalGenerator = get_bam_data(i_dnaNormalFilename, i_dnaNormalFastaFilename, i_dnaNormBaseQual, i_dnaNormMapQual, i_dnaNormMitochon, i_startCoordinate, i_stopCoordinate, i_batchSize, i_dnaNormUseChr, i_dnaNormLabel, i_debug)                      
        else:
            i_dnaNormalGenerator = get_bam_data(i_dnaNormalFilename, i_dnaNormalFastaFilename, i_dnaNormBaseQual, i_dnaNormMapQual, i_chrom, i_startCoordinate, i_stopCoordinate, i_batchSize, i_dnaNormUseChr, i_dnaNormLabel, i_debug)                      

    if (i_rnaNormalFilename != None):
        # some bams/reference use "M", some use "MT"
        if (i_chrom == "M" or i_chrom == "MT" and i_rnaNormMitochon != None):
            i_rnaNormalGenerator = get_bam_data(i_rnaNormalFilename, i_rnaNormalFastaFilename, i_rnaNormBaseQual, i_rnaNormMapQual, i_rnaNormMitochon, i_startCoordinate, i_stopCoordinate, i_batchSize, i_rnaNormUseChr, i_rnaNormLabel, i_debug)
        else:
            i_rnaNormalGenerator = get_bam_data(i_rnaNormalFilename, i_rnaNormalFastaFilename, i_rnaNormBaseQual, i_rnaNormMapQual, i_chrom, i_startCoordinate, i_stopCoordinate, i_batchSize, i_rnaNormUseChr, i_rnaNormLabel, i_debug)

    if (i_dnaTumorFilename != None):
        # some bams/reference use "M", some use "MT"
        if (i_chrom == "M" or i_chrom == "MT" and i_dnaTumMitochon != None):
            i_dnaTumorGenerator = get_bam_data(i_dnaTumorFilename, i_dnaTumorFastaFilename, i_dnaTumBaseQual, i_dnaTumMapQual, i_dnaTumMitochon, i_startCoordinate, i_stopCoordinate, i_batchSize, i_dnaTumUseChr, i_dnaTumLabel, i_debug)
        else:
            i_dnaTumorGenerator = get_bam_data(i_dnaTumorFilename, i_dnaTumorFastaFilename, i_dnaTumBaseQual, i_dnaTumMapQual, i_chrom, i_startCoordinate, i_stopCoordinate, i_batchSize, i_dnaTumUseChr, i_dnaTumLabel, i_debug)   
    
    if (i_rnaTumorFilename != None):
        # some bams/reference use "M", some use "MT"
        if (i_chrom == "M" or i_chrom == "MT" and i_rnaTumMitochon != None):
            i_rnaTumorGenerator = get_bam_data(i_rnaTumorFilename, i_rnaTumorFastaFilename, i_rnaTumBaseQual, i_rnaTumMapQual, i_rnaTumMitochon, i_startCoordinate, i_stopCoordinate, i_batchSize, i_rnaTumUseChr, i_rnaTumLabel, i_debug)
        else:
            i_rnaTumorGenerator = get_bam_data(i_rnaTumorFilename, i_rnaTumorFastaFilename, i_rnaTumBaseQual, i_rnaTumMapQual, i_chrom, i_startCoordinate, i_stopCoordinate, i_batchSize, i_rnaTumUseChr, i_rnaTumLabel, i_debug)


    # create the VCF header 
    platforms = [i_sequencingPlatform] * len(filenames)
    sources = [i_dataSource] * len(filenames)
    # we don't want the start and stop coordinates in the header
    del i_cmdLineOptionsDict["startCoordinate"]
    del i_cmdLineOptionsDict["stopCoordinate"]
    vcfHeader = get_vcf_header(i_vcfFormat, i_refId, i_refUrl, i_refFilename, i_universalFastaFilename, i_radiaVersion, i_id, i_cmdLineOptionsDict, filenames, labels, descriptions, platforms, sources, i_disease, i_debug)
    
    # open the output stream
    i_outputFileHandler = None
    if (i_outputFilename != None):
        i_outputFileHandler = get_write_fileHandler(i_outputFilename)
        # output vcf meta information
        i_outputFileHandler.write(vcfHeader + "\n")
    else:
        # output vcf meta information
        print >> sys.stdout, vcfHeader
    
    startTime = time.time()
     
    # for each coordinate
        # if we have normal dna
            # compare to reference -> germline mutations
            
        # if we have normal rna-seq
            # characterize germline variants
            # identify normal rna-editing
            
        # if we have tumor dna
            # compare to reference and normal -> somatic mutations
        
        # if we have tumor rna-seq
            # characterize somatic mutations
            # identify tumor rna-editing

    
    # get the first pileup from each file
    # if a file is not specified, then the "moreLines" flags will be set to false and initial values will be returned
    (moreDnaNormalLines, dnaNormalChr, dnaNormalCoordinate, dnaNormalRefBase, dnaNormalNumBases, dnaNormalReads, dnaNormalQualScores) = get_next_pileup(i_dnaNormalGenerator)
    (moreRnaNormalLines, rnaNormalChr, rnaNormalCoordinate, rnaNormalRefBase, rnaNormalNumBases, rnaNormalReads, rnaNormalQualScores) = get_next_pileup(i_rnaNormalGenerator)
    (moreDnaTumorLines, dnaTumorChr, dnaTumorCoordinate, dnaTumorRefBase, dnaTumorNumBases, dnaTumorReads, dnaTumorQualScores) = get_next_pileup(i_dnaTumorGenerator)
    (moreRnaTumorLines, rnaTumorChr, rnaTumorCoordinate, rnaTumorRefBase, rnaTumorNumBases, rnaTumorReads, rnaTumorQualScores) = get_next_pileup(i_rnaTumorGenerator)
    
    # initialize some variables
    formatString = "GT:DP:INDEL:START:STOP:AD:AF:BQ:SB"
    countRnaDnaCoordinateOverlap = 0
    totalGerms = 0
    totalSoms = 0
    totalNormEdits = 0
    totalTumEdits = 0
    totalNoRef = 0
    totalLohs = 0
    totalNormNotExp = 0
    totalTumNotExp = 0
    countRefMismatches = 0
    dnaSet = set()
    altList = list()
    refList = list()
    filterList = list()
    coordinateBaseQualsList = list()
    altCountsDict = collections.defaultdict(int)
    infoDict = collections.defaultdict(list)
    
    dnaNormalReadDPDict = collections.defaultdict(int)
    rnaNormalReadDPDict = collections.defaultdict(int)
    dnaTumorReadDPDict = collections.defaultdict(int)
    rnaTumorReadDPDict = collections.defaultdict(int)
    
    dnaNormalAltPercentDict = collections.defaultdict(int)
    rnaNormalAltPercentDict = collections.defaultdict(int)
    dnaTumorAltPercentDict = collections.defaultdict(int)
    rnaTumorAltPercentDict = collections.defaultdict(int)
    
    dnaNormalCoordinateWithData = 0
    dnaTumorCoordinateWithData = 0
    rnaNormalCoordinateWithData = 0
    rnaTumorCoordinateWithData = 0
    
    # this only needs to be initialized once for the first pass through filterVariants
    # filterVariants() creates a new baseCounts dict for each sample and returns it 
    # these are used for 2 purposes:
    # 1) to make a call when the previous sample didn't have enough bases for a call (not > aMinAltNumBases)
    # 2) to determine the bases that were lost on an LOH call 
    previousBaseCounts = collections.defaultdict(int)
    dnaNormalPreviousBaseCounts = collections.defaultdict(int)
    
    # for each coordinate that we'd like to investigate
    for currentCoordinate in xrange(i_startCoordinate, i_stopCoordinate):
        
        if (i_debug):
            logging.debug("currentCoordinate: %s", currentCoordinate)
            logging.debug("Initial NormalDNAData: %s %s %s %s %s %s", dnaNormalChr, dnaNormalCoordinate, dnaNormalRefBase, dnaNormalNumBases, dnaNormalReads, dnaNormalQualScores)
            logging.debug("Initial NormalRNAData: %s %s %s %s %s %s", rnaNormalChr, rnaNormalCoordinate, rnaNormalRefBase, rnaNormalNumBases, rnaNormalReads, rnaNormalQualScores)
            logging.debug("Initial TumorDNAData: %s %s %s %s %s %s", dnaTumorChr, dnaTumorCoordinate, dnaTumorRefBase, dnaTumorNumBases, dnaTumorReads, dnaTumorQualScores)
            logging.debug("Initial TumorRNAData: %s %s %s %s %s %s", rnaTumorChr, rnaTumorCoordinate, rnaTumorRefBase, rnaTumorNumBases, rnaTumorReads, rnaTumorQualScores)
            
        # empty the set of DNA for each new coordinate
        dnaSet.clear()
        altCountsDict.clear()
        infoDict.clear()
        del altList[:]
        del refList[:]
        del filterList[:]
        del coordinateBaseQualsList[:]
        
        setMinTotalBasesFlag = True
        setMinAltBasesFlag = True
        shouldOutput = False
        hasDNA = False
        hasRNA = False
        totalSamples = 0
        totalReadDepth = 0
        totalIndels = 0
        totalStarts = 0
        totalStops = 0
        totalSumBaseQual = 0
        totalSumStrandBias = 0
        totalAltReadDepth = 0
        
        # create some default output in case there are no reads for one dataset but there are for others
        #columnHeaders = ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT"]
        vcfOutputList = [i_chrom, str(currentCoordinate), "."]
        dnaNormalOutputString = "."
        dnaTumorOutputString = "."
        rnaNormalOutputString = "."
        rnaTumorOutputString = "."
        
        # these are only used to determine the Germline parent of an LOH
        # the output shows which parent base has been lost in the tumor DNA
        # we need the 2 variables, b/c we want to skip over the normal RNA
        # and pass the normal DNA previous bases onto the tumor DNA to look
        # for an LOH.  in the future, we may want to output other "losses",
        # but for now, the previousUniqueBases is just a place-holder.
        previousUniqueBases = ""
        dnaNormalPreviousBases = ""
    
        # create the ref list for this coordinate
        if (dnaNormalCoordinate == currentCoordinate and dnaNormalRefBase not in refList):
            refList.append(dnaNormalRefBase)
        
        if (rnaNormalCoordinate == currentCoordinate and rnaNormalRefBase not in refList):
            refList.append(rnaNormalRefBase)
        
        if (dnaTumorCoordinate == currentCoordinate and dnaTumorRefBase not in refList):
            refList.append(dnaTumorRefBase)
        
        if (rnaTumorCoordinate == currentCoordinate and rnaTumorRefBase not in refList):
            refList.append(rnaTumorRefBase)
        
        # if we aren't debugging and we have an "N" in the ref or more than one ref, then just ignore this coordinate and move on to the next
        if (not i_debug and ("N" in refList or len(refList) > 1)):
            # if there are more lines, and the coordinate is <= the current coordinate, then get the next pileup
            if (moreDnaNormalLines and dnaNormalCoordinate <= currentCoordinate):
                (moreDnaNormalLines, dnaNormalChr, dnaNormalCoordinate, dnaNormalRefBase, dnaNormalNumBases, dnaNormalReads, dnaNormalQualScores) = get_next_pileup(i_dnaNormalGenerator)                      
            
            if (moreRnaNormalLines and rnaNormalCoordinate <= currentCoordinate):
                (moreRnaNormalLines, rnaNormalChr, rnaNormalCoordinate, rnaNormalRefBase, rnaNormalNumBases, rnaNormalReads, rnaNormalQualScores) = get_next_pileup(i_rnaNormalGenerator)                   
            
            if (moreDnaTumorLines and dnaTumorCoordinate <= currentCoordinate):
                (moreDnaTumorLines, dnaTumorChr, dnaTumorCoordinate, dnaTumorRefBase, dnaTumorNumBases, dnaTumorReads, dnaTumorQualScores) = get_next_pileup(i_dnaTumorGenerator)                      
                   
            if (moreRnaTumorLines and rnaTumorCoordinate <= currentCoordinate):
                (moreRnaTumorLines, rnaTumorChr, rnaTumorCoordinate, rnaTumorRefBase, rnaTumorNumBases, rnaTumorReads, rnaTumorQualScores) = get_next_pileup(i_rnaTumorGenerator)
            
            # continue to the next coordinate
            continue;
                        
        # if we have normal reads at the current position
        if (dnaNormalCoordinate == currentCoordinate):
            
            # specify the normal constants
            gainModType = "GERM"
            lossModType = "NOREF"
        
            # process the normal DNA
            (dnaNormalOutputString, dnaNormalPreviousBases, dnaNormalPreviousBaseCounts, dnaNormalReadDPDict, dnaNormalAltPercentDict, dnaNormalCoordinateWithData, dnaSet, altList, altCountsDict, hasDNA, shouldOutput, numTotalBasesFilter, numAltBasesFilter, totalGerms, totalNoRef, infoDict, numBases, indels, starts, stops, totalBaseQual, totalStrandBias, totalAltReadSupport, coordinateBaseQualsList) = find_variants(dnaNormalChr, dnaNormalCoordinate, dnaNormalRefBase, dnaNormalNumBases, dnaNormalReads, dnaNormalQualScores, previousUniqueBases, previousBaseCounts, dnaNormalReadDPDict, dnaNormalAltPercentDict, dnaNormalCoordinateWithData, dnaSet, refList, altList, altCountsDict, hasDNA, shouldOutput, totalGerms, totalNoRef, gainModType, lossModType, infoDict, i_dnaNormMinTotalNumBases, i_dnaNormMinAltNumBases, i_dnaNormBaseQual, coordinateBaseQualsList, "DNA_NORMAL", i_genotypeMinDepth, i_genotypeMinPct, i_debug)
            
            if (numBases > 0):
                totalSamples += 1
                totalReadDepth += numBases
                totalIndels += indels
                totalStarts += starts
                totalStops += stops
                totalSumBaseQual += totalBaseQual
                totalSumStrandBias += totalStrandBias
                totalAltReadDepth += totalAltReadSupport
                setMinTotalBasesFlag = (setMinTotalBasesFlag and numTotalBasesFilter)
                setMinAltBasesFlag = (setMinAltBasesFlag and numAltBasesFilter)
                
        # if we have normal rna-seq reads at the current position
        if (rnaNormalCoordinate == currentCoordinate):
            
            # if either a normal or tumor file is specified, we will label them as edits
            # if neither a normal file nor a tumor file is specified, we will label them as variants
            if (i_dnaNormalFilename == None and i_dnaTumorFilename == None):
                gainModType = "RNA_NOR_VAR"
            else:
                gainModType = "NOR_EDIT"    
            lossModType = "NOTEXP"
            
            # this is temporary, b/c we don't want to output NOTEXP right now
            # need to think about this in more detail
            previousUniqueBases = ""
            
            (rnaNormalOutputString, previousUniqueBases, previousBaseCounts, rnaNormalReadDPDict, rnaNormalAltPercentDict, rnaNormalCoordinateWithData, dnaSet, altList, altCountsDict, hasRNA, shouldOutput, numTotalBasesFilter, numAltBasesFilter, totalNormEdits, totalNormNotExp, infoDict, numBases, indels, starts, stops, totalBaseQual, totalStrandBias, totalAltReadSupport, coordinateBaseQualsList) = find_variants(rnaNormalChr, rnaNormalCoordinate, rnaNormalRefBase, rnaNormalNumBases, rnaNormalReads, rnaNormalQualScores, previousUniqueBases, previousBaseCounts, rnaNormalReadDPDict, rnaNormalAltPercentDict, rnaNormalCoordinateWithData, dnaSet, refList, altList, altCountsDict, hasRNA, shouldOutput, totalNormEdits, totalNormNotExp, gainModType, lossModType, infoDict, i_rnaNormMinTotalNumBases, i_rnaNormMinAltNumBases, i_rnaNormBaseQual, coordinateBaseQualsList, "RNA_NORMAL", i_genotypeMinDepth, i_genotypeMinPct, i_debug)    
            
            if (numBases > 0):
                totalSamples += 1
                totalReadDepth += numBases
                totalIndels += indels
                totalStarts += starts
                totalStops += stops
                totalSumBaseQual += totalBaseQual
                totalSumStrandBias += totalStrandBias
                totalAltReadDepth += totalAltReadSupport
                setMinTotalBasesFlag = (setMinTotalBasesFlag and numTotalBasesFilter)
                setMinAltBasesFlag = (setMinAltBasesFlag and numAltBasesFilter)
                
        # if we have tumor reads at the current position
        if (dnaTumorCoordinate == currentCoordinate):
                
            # if a normal file is specified, we will label them as somatic mutations
            # otherwise, we will just call them variants
            if (i_dnaNormalFilename != None):
                gainModType = "SOM"
            else:
                gainModType = "DNA_TUM_VAR"
            lossModType = "LOH"
            
            # process the tumor DNA
            (dnaTumorOutputString, previousUniqueBases, previousBaseCounts, dnaTumorReadDPDict, dnaTumorAltPercentDict, dnaTumorCoordinateWithData, dnaSet, altList, altCountsDict, hasDNA, shouldOutput, numTotalBasesFilter, numAltBasesFilter, totalSoms, totalLohs, infoDict, numBases, indels, starts, stops, totalBaseQual, totalStrandBias, totalAltReadSupport, coordinateBaseQualsList) = find_variants(dnaTumorChr, dnaTumorCoordinate, dnaTumorRefBase, dnaTumorNumBases, dnaTumorReads, dnaTumorQualScores, dnaNormalPreviousBases, dnaNormalPreviousBaseCounts, dnaTumorReadDPDict, dnaTumorAltPercentDict, dnaTumorCoordinateWithData, dnaSet, refList, altList, altCountsDict, hasDNA, shouldOutput, totalSoms, totalLohs, gainModType, lossModType, infoDict, i_dnaTumMinTotalNumBases, i_dnaTumMinAltNumBases, i_dnaTumBaseQual, coordinateBaseQualsList, "DNA_TUMOR", i_genotypeMinDepth, i_genotypeMinPct, i_debug)
            
            if (numBases > 0):
                totalSamples += 1
                totalReadDepth += numBases
                totalIndels += indels
                totalStarts += starts
                totalStops += stops
                totalSumBaseQual += totalBaseQual
                totalSumStrandBias += totalStrandBias
                totalAltReadDepth += totalAltReadSupport
                setMinTotalBasesFlag = (setMinTotalBasesFlag and numTotalBasesFilter)
                setMinAltBasesFlag = (setMinAltBasesFlag and numAltBasesFilter)
            
        # if we have tumor rna-seq reads at the current position
        if (rnaTumorCoordinate == currentCoordinate):
            
            # if either a normal or tumor file is specified, we will label them as edits
            # if neither a normal file nor a tumor file is specified, we will label them as variants
            if (i_dnaNormalFilename == None and i_dnaTumorFilename == None):
                gainModType = "RNA_TUM_VAR"
            else:
                gainModType = "TUM_EDIT"
            lossModType = "NOTEXP"
            
            # this is temporary, b/c we don't want to output NOTEXP right now
            # need to think about this in more detail
            previousUniqueBases = ""
            
            (rnaTumorOutputString, previousUniqueBases, previousBaseCounts, rnaTumorReadDPDict, rnaTumorAltPercentDict, rnaTumorCoordinateWithData, dnaSet, altList, altCountsDict, hasRNA, shouldOutput, numTotalBasesFilter, numAltBasesFilter, totalTumEdits, totalTumNotExp, infoDict, numBases, indels, starts, stops, totalBaseQual, totalStrandBias, totalAltReadSupport, coordinateBaseQualsList) = find_variants(rnaTumorChr, rnaTumorCoordinate, rnaTumorRefBase, rnaTumorNumBases, rnaTumorReads, rnaTumorQualScores, previousUniqueBases, previousBaseCounts, rnaTumorReadDPDict, rnaTumorAltPercentDict, rnaTumorCoordinateWithData, dnaSet, refList, altList, altCountsDict, hasRNA, shouldOutput, totalTumEdits, totalTumNotExp, gainModType, lossModType, infoDict, i_rnaTumMinTotalNumBases, i_rnaTumMinAltNumBases, i_rnaTumBaseQual, coordinateBaseQualsList, "RNA_TUMOR", i_genotypeMinDepth, i_genotypeMinPct, i_debug)    
            
            if (numBases > 0):
                totalSamples += 1
                totalReadDepth += numBases
                totalIndels += indels
                totalStarts += starts
                totalStops += stops
                totalSumBaseQual += totalBaseQual
                totalSumStrandBias += totalStrandBias
                totalAltReadDepth += totalAltReadSupport
                setMinTotalBasesFlag = (setMinTotalBasesFlag and numTotalBasesFilter)
                setMinAltBasesFlag = (setMinAltBasesFlag and numAltBasesFilter)
            
        # count the number of ref mismatches
        if (len(refList) > 1):
            countRefMismatches += 1
            
        # if we should output
        if (shouldOutput or i_debug):
            
            # the chrom, position, and Id columns have been filled
            #columnHeaders = ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT"]
            
            # add the ref, alt, and score
            vcfOutputList.append(",".join(refList))
            vcfOutputList.append(",".join(altList))
            vcfOutputList.append("0")
            
            # add filters
            # if one of the references is "N", then set the filter
            if ("N" in refList):
                filterList.append("noref")
            # if there is more than one reference, then set the filter
            if (len(refList) > 1):   
                filterList.append("diffref")  
            # if there aren't enough total bases, then set the filter    
            if (setMinTotalBasesFlag):
                filterList.append("mbt")
            # if there aren't enough ALT bases, then set the filter   
            if (setMinAltBasesFlag):
                filterList.append("mba")    
            # if there are no filters thus far, then pass it    
            if (len(filterList) == 0):
                filterList.append("PASS")
            
            # if we pass the basic filters, or if we are debugging    
            if (("PASS" in filterList and shouldOutput) or (i_debug)):
                vcfOutputList.append(";".join(filterList))
                
                #vcfHeader += "##INFO=<ID=NS,Number=1,Type=Integer,Description=\"Number of samples with data\">\n"
                #vcfHeader += "##INFO=<ID=AN,Number=1,Type=Integer,Description=\"Total number of unique alleles across all samples\">\n"
                #vcfHeader += "##INFO=<ID=DP,Number=1,Type=Integer,Description=\"Total read depth for all samples\">\n"
                #vcfHeader += "##INFO=<ID=INDEL,Number=1,Type=Integer,Description=\"Number of indels for all samples\">\n"
                #vcfHeader += "##INFO=<ID=START,Number=1,Type=Integer,Description=\"Number of reads that started at this position across all samples\">\n"
                #vcfHeader += "##INFO=<ID=STOP,Number=1,Type=Integer,Description=\"Number of reads that stopped at this position across all samples\">\n"
                #vcfHeader += "##INFO=<ID=INDEL,Number=1,Type=Integer,Description=\"Number of indels for all samples\">\n"
                #vcfHeader += "##INFO=<ID=BQ,Number=1,Type=Float,Description=\"Overall average base quality\">\n"
                #vcfHeader += "##INFO=<ID=SB,Number=1,Type=Float,Description=\"Overall average reads on plus strand\">\n"
                #vcfHeader += "##INFO=<ID=FA,Number=1,Type=Float,Description=\"Overall fraction of reads supporting ALT\">\n"
                #vcfHeader += "##INFO=<ID=MT,Number=.,Type=String,Description=\"Modification types at this position\">\n"
                #vcfHeader += "##INFO=<ID=MC,Number=.,Type=String,Description=\"Modification base changes at this position\">\n"
                
                # add the alt counts and frequencies in the same order as the alt list 
                for base in altList:
                    infoDict["AC"].append(str(altCountsDict[base]))
                    infoDict["AF"].append(str(round(altCountsDict[base]/float(totalReadDepth),2)))
                
                # add modTypes to info
                infoDict["NS"].append(str(totalSamples))
                infoDict["AN"].append(str(len(dnaSet)))
                infoDict["DP"].append(str(totalReadDepth))
                infoDict["INDEL"].append(str(totalIndels))
                infoDict["START"].append(str(totalStarts))
                infoDict["STOP"].append(str(totalStops))
                infoDict["VT"].append("SNP")
                if (totalReadDepth > 0):
                    infoDict["BQ"].append(str(round(totalSumBaseQual/float(totalReadDepth),2)))
                    infoDict["SB"].append(str(round(totalSumStrandBias/float(totalReadDepth),2)))
                    infoDict["FA"].append(str(round(totalAltReadDepth/float(totalReadDepth),2)))
                
                # add info
                infoField = ""
                for key in sorted(infoDict.iterkeys()):
                    if ("True" in infoDict[key]):
                        infoField += key + ";"
                    else:    
                        infoField += key + "=" + ",".join(infoDict[key]) + ";"
                
                vcfOutputList.append(infoField.rstrip(";"))
                
                # add format
                vcfOutputList.append(formatString)
                
                # add the sample specific data
                if (i_dnaNormalFilename != None):
                    dnaNormalOutputString = pad_output(dnaNormalOutputString, len(refList + altList))
                    vcfOutputList.append(dnaNormalOutputString)
                if (i_rnaNormalFilename != None):
                    rnaNormalOutputString = pad_output(rnaNormalOutputString, len(refList + altList))
                    vcfOutputList.append(rnaNormalOutputString)
                if (i_dnaTumorFilename != None):
                    dnaTumorOutputString = pad_output(dnaTumorOutputString, len(refList + altList))
                    vcfOutputList.append(dnaTumorOutputString)
                if (i_rnaTumorFilename != None):
                    rnaTumorOutputString = pad_output(rnaTumorOutputString, len(refList + altList))
                    vcfOutputList.append(rnaTumorOutputString)
                
                # output
                if ("PASS" not in filterList and i_debug):
                    # output the line if we're debugging
                    logging.debug("\t".join(vcfOutputList))
                elif (shouldOutput):
                    if (i_outputFileHandler != None):
                        i_outputFileHandler.write("\t".join(vcfOutputList) + "\n")
                    else:
                        print >> sys.stdout, "\t".join(vcfOutputList)
                        
                    if (i_debug):
                        logging.debug("finalOutput: %s", "\t".join(vcfOutputList))    
            
        # count coordinates when we have both DNA and RNA
        if (hasDNA and hasRNA):
            countRnaDnaCoordinateOverlap += 1
        
        # if there are more lines, and the coordinate is <= the current coordinate, then get the next pileup
        if (moreDnaNormalLines and dnaNormalCoordinate <= currentCoordinate):
            (moreDnaNormalLines, dnaNormalChr, dnaNormalCoordinate, dnaNormalRefBase, dnaNormalNumBases, dnaNormalReads, dnaNormalQualScores) = get_next_pileup(i_dnaNormalGenerator)                      
        if (moreRnaNormalLines and rnaNormalCoordinate <= currentCoordinate):
            (moreRnaNormalLines, rnaNormalChr, rnaNormalCoordinate, rnaNormalRefBase, rnaNormalNumBases, rnaNormalReads, rnaNormalQualScores) = get_next_pileup(i_rnaNormalGenerator)                   
        if (moreDnaTumorLines and dnaTumorCoordinate <= currentCoordinate):
            (moreDnaTumorLines, dnaTumorChr, dnaTumorCoordinate, dnaTumorRefBase, dnaTumorNumBases, dnaTumorReads, dnaTumorQualScores) = get_next_pileup(i_dnaTumorGenerator)                      
        if (moreRnaTumorLines and rnaTumorCoordinate <= currentCoordinate):
            (moreRnaTumorLines, rnaTumorChr, rnaTumorCoordinate, rnaTumorRefBase, rnaTumorNumBases, rnaTumorReads, rnaTumorQualScores) = get_next_pileup(i_rnaTumorGenerator)   
    
    if (i_statsDir != None):
        # output the variant counts
        i_variantCountsFileHandler = open(i_statsDir + "variantCounts.tab", "a")
        #i_variantCountsFileHandler.write(i_id + "\t" + i_chrom + "\t" + str(totalGerms) + "\t" + str(totalSoms) + "\t" + str(totalNormEdits) + "\t" + str(totalTumEdits) + "\t" + str(totalLohs) + "\n")
        i_variantCountsFileHandler.write(i_id + "\t" + i_chrom + "\t" + str(totalGerms) + "\t" + str(totalSoms) + "\t" + str(totalNormEdits) + "\t" + str(totalTumEdits) + "\n")
        i_variantCountsFileHandler.close()
          
        # output the coordinates with data
        i_genStatsFileHandler = open(i_statsDir + "genStats.tab", "a")
        i_genStatsFileHandler.write(i_id + "\t" + i_chrom + "\t" + str(dnaNormalCoordinateWithData) + "\t" + str(rnaNormalCoordinateWithData) + "\t" + str(dnaTumorCoordinateWithData) + "\t" + str(rnaTumorCoordinateWithData) + "\n")
        i_genStatsFileHandler.close()
    
    stopTime = time.time()  
    
    summaryMessage = "Summary for Chrom " + i_chrom + " and Id " + i_id + ": "
    if (i_dnaNormalFilename != None):
        #summaryMessage += "Total GERMs=" + str(totalGerms-totalLohs) + ", "
        summaryMessage += "Total GERMs=" + str(totalGerms) + ", "
    if (i_rnaNormalFilename != None):
        summaryMessage += "Total Normal EDITs=" + str(totalNormEdits) + ", "
    if (i_dnaTumorFilename != None):
        summaryMessage += "Total SOMs=" + str(totalSoms) + ", "
        #summaryMessage += "Total LOHs=" + str(totalLohs) + ", "
    if (i_rnaTumorFilename != None):
        summaryMessage += "Total Tumor EDITs=" + str(totalTumEdits) + ", "
    #summaryMessage += "Total coordinates with both DNA and RNA=" + str(countRnaDnaCoordinateOverlap) + ", "
    
    logging.info(summaryMessage.rstrip(", "))
    logging.info("Chrom %s and Id %s: Total time=%s hrs, %s mins, %s secs", i_chrom, i_id, ((stopTime-startTime)/(3600)), ((stopTime-startTime)/60), (stopTime-startTime)) 
           
    # close the files 
    if (i_outputFilename != None):
        i_outputFileHandler.close()
    return
 
 
main()    
sys.exit(0)
